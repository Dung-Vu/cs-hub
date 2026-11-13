"""hub_auth.py — Odoo-account login (password + 2FA) for the whole CS Hub.

Same rules as label.bonstu.site:
- Only internal Odoo users (active, share = False) with 2FA enabled can log in.
- The password + TOTP code are verified by replaying the standard Odoo 19 web
  login flow server-side; the Odoo web session is logged out right after, so the
  hub never keeps an Odoo session or a password.
- The hub session is a signed Flask cookie that slides: every request re-issues
  it for another SESSION_DAYS, so people who keep using the hub never have to
  log in again; only SESSION_DAYS without any visit (or an archived Odoo
  account, re-checked once a day) ends it. Rotating HUB_SECRET_KEY logs
  everyone out — keep it stable.

Pending logins (between the password step and the 2FA step) and login rate
limits live in a small SQLite file so they work across gunicorn workers.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Callable

from flask import (Blueprint, Flask, jsonify, redirect, render_template,
                   request, session, url_for)

SESSION_DAYS = 90
RECHECK_SECONDS = 24 * 3600
PENDING_TTL = 10 * 60
MAX_TOTP_ATTEMPTS = 5
LOGIN_WINDOW = 15 * 60
MAX_LOGINS_PER_WINDOW = 10
PENDING_COOKIE = "hub_login_pending"

PUBLIC_PATHS = {"/login", "/api/health"}
PUBLIC_PREFIXES = ("/api/auth/", "/static/")


class OdooLoginError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


ERROR_STATUS = {
    "invalid_credentials": 401,
    "invalid_totp": 401,
    "session_expired": 401,
    "cooldown": 429,
    "rate_limited": 429,
}


# ── Odoo web login flow ─────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


class OdooWebLogin:
    """Replays /web/session/authenticate → /web/login/totp like a browser."""

    def __init__(self, base_url: str, db: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.timeout = timeout

    # A fresh opener per login so Odoo cookies never leak between users.
    def _opener(self, session_id: str | None = None):
        jar = http.cookiejar.CookieJar()
        if session_id:
            host = urllib.parse.urlparse(self.base_url).hostname or ""
            jar.set_cookie(http.cookiejar.Cookie(
                0, "session_id", session_id, None, False, host, False, False,
                "/", True, False, None, False, None, None, {}))
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
        return opener, jar

    @staticmethod
    def _session_id(jar) -> str | None:
        for cookie in jar:
            if cookie.name == "session_id":
                return cookie.value
        return None

    def _open(self, opener, path: str, data: bytes | None = None,
              headers: dict | None = None):
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=headers or {})
        try:
            resp = opener.open(req, timeout=self.timeout)
            return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:  # 3xx (no redirect) / 4xx / 5xx
            if e.code == 429:
                raise OdooLoginError(
                    "rate_limited", "Odoo đang bận, vui lòng thử lại sau ít phút.")
            return e.code, e.headers, e.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            raise OdooLoginError(
                "odoo_error", "Không kết nối được Odoo, vui lòng thử lại sau.")

    def _jsonrpc(self, opener, path: str, params: dict):
        body = json.dumps({"jsonrpc": "2.0", "method": "call",
                           "params": params, "id": int(time.time())}).encode()
        status, _, raw = self._open(opener, path, body,
                                    {"Content-Type": "application/json"})
        try:
            payload = json.loads(raw)
        except ValueError:
            raise OdooLoginError(
                "odoo_error", "Odoo không phản hồi đúng, vui lòng thử lại sau.")
        if payload.get("error"):
            raise _map_auth_error(payload["error"])
        return payload.get("result")

    def _logout(self, opener):
        try:
            self._open(opener, "/web/session/logout")
        except OdooLoginError:
            pass  # best effort, the Odoo session expires on its own

    def start_login(self, login: str, password: str) -> dict:
        """Step 1 → {'mfa_required': True, 'session_id': …} or {'mfa_required': False}."""
        opener, jar = self._opener()
        result = self._jsonrpc(opener, "/web/session/authenticate",
                               {"db": self.db, "login": login, "password": password})
        if isinstance(result, dict) and result.get("uid"):
            self._logout(opener)
            return {"mfa_required": False, "uid": result["uid"]}
        if isinstance(result, dict) and "uid" in result and result["uid"] is None:
            sid = self._session_id(jar)
            if sid:
                return {"mfa_required": True, "session_id": sid}
        raise OdooLoginError(
            "odoo_error", "Odoo không phản hồi đúng, vui lòng thử lại sau.")

    def verify_totp(self, session_id: str, code: str) -> int:
        """Step 2 → Odoo uid once the 2FA code is accepted."""
        opener, _ = self._opener(session_id)
        status, _, html = self._open(opener, "/web/login/totp")
        if 300 <= status < 400:
            raise OdooLoginError(
                "session_expired", "Phiên đăng nhập đã hết hạn, vui lòng nhập lại mật khẩu.")
        page = html.decode("utf-8", "replace")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', page)
        if not m:
            raise OdooLoginError("odoo_error", "Không đọc được form 2FA của Odoo.")
        form = urllib.parse.urlencode({"csrf_token": m.group(1),
                                       "totp_token": code}).encode()
        status, headers, html = self._open(
            opener, "/web/login/totp", form,
            {"Content-Type": "application/x-www-form-urlencoded"})
        location = (headers or {}).get("Location", "") or ""
        if not (300 <= status < 400) or "/web/login" in location:
            raise OdooLoginError(
                "invalid_totp",
                _form_error(html.decode("utf-8", "replace")) or "Mã xác thực không đúng.")
        info = self._jsonrpc(opener, "/web/session/get_session_info", {})
        self._logout(opener)
        if not (isinstance(info, dict) and info.get("uid")):
            raise OdooLoginError(
                "odoo_error", "Odoo không xác nhận được phiên đăng nhập.")
        return int(info["uid"])


def _map_auth_error(error: dict) -> OdooLoginError:
    data = error.get("data") or {}
    message = f"{data.get('name', '')} {data.get('message', '')} {error.get('message', '')}"
    if re.search(r"too many login failures|cooldown", message, re.I):
        return OdooLoginError(
            "cooldown", "Đăng nhập sai quá nhiều lần, vui lòng chờ 1 phút rồi thử lại.")
    if re.search(r"AccessDenied|access denied|wrong login|password", message, re.I):
        return OdooLoginError("invalid_credentials", "Sai email hoặc mật khẩu Odoo.")
    return OdooLoginError(
        "odoo_error", "Odoo không phản hồi đúng, vui lòng thử lại sau.")


def _form_error(html: str) -> str | None:
    m = re.search(r'class="alert alert-danger[^"]*"[^>]*>([\s\S]*?)</(?:p|div)>', html)
    if not m:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip() or None


# ── Shared state across gunicorn workers ────────────────────────────────────

class AuthStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY,"
                       " session_id TEXT, login TEXT, created REAL, attempts INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS attempts (ip TEXT, ts REAL)")

    def _db(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def too_many_logins(self, ip: str, now: float) -> bool:
        with self._db() as db:
            db.execute("DELETE FROM attempts WHERE ts < ?", (now - LOGIN_WINDOW,))
            db.execute("INSERT INTO attempts VALUES (?, ?)", (ip, now))
            (count,) = db.execute("SELECT COUNT(*) FROM attempts WHERE ip = ?",
                                  (ip,)).fetchone()
        return count > MAX_LOGINS_PER_WINDOW

    def create_pending(self, session_id: str, login: str, now: float) -> str:
        pid = secrets.token_urlsafe(24)
        with self._db() as db:
            db.execute("DELETE FROM pending WHERE created < ?", (now - PENDING_TTL,))
            db.execute("INSERT INTO pending VALUES (?, ?, ?, ?, 0)",
                       (pid, session_id, login, now))
        return pid

    def take_attempt(self, pid: str, now: float) -> dict | None:
        """Increments the attempt counter and returns the pending entry."""
        with self._db() as db:
            row = db.execute("SELECT session_id, login, created, attempts FROM pending"
                             " WHERE id = ?", (pid,)).fetchone()
            if not row:
                return None
            if now - row[2] > PENDING_TTL:
                db.execute("DELETE FROM pending WHERE id = ?", (pid,))
                return None
            db.execute("UPDATE pending SET attempts = attempts + 1 WHERE id = ?", (pid,))
        return {"session_id": row[0], "login": row[1], "attempts": row[3] + 1}

    def drop_pending(self, pid: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM pending WHERE id = ?", (pid,))


# ── Flask wiring ────────────────────────────────────────────────────────────

def _client_ip() -> str:
    return (request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or request.remote_addr or "?")


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def init_auth(app: Flask, *, get_odoo: Callable[[], dict],
              rpc: Callable[..., object]) -> None:
    """Registers login routes and the login gate on every other route."""
    secret = os.environ.get("HUB_SECRET_KEY", "")
    if len(secret) < 32:
        raise RuntimeError("HUB_SECRET_KEY (>= 32 ký tự) chưa được cấu hình trong .env")
    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_NAME="hub_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("HUB_COOKIE_SECURE", "1") != "0",
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
        SESSION_REFRESH_EACH_REQUEST=True,  # sliding expiry, see module docstring
    )
    store = AuthStore(os.environ.get("HUB_AUTH_DB", "/tmp/cs-hub-auth.sqlite3"))
    clock = time.time

    def web_login() -> OdooWebLogin:
        conn = get_odoo()
        return OdooWebLogin(conn["baseUrl"], conn["db"])

    def read_user(uid: int) -> dict | None:
        rows = rpc(get_odoo(), "res.users", "search_read",
                   [("id", "=", uid)], fields=["name", "login", "active", "share"],
                   context={"active_test": False})
        return rows[0] if rows else None

    def is_internal(user: dict | None) -> bool:
        return bool(user and user.get("active") and not user.get("share"))

    def odoo_error(error: Exception):
        if not isinstance(error, OdooLoginError):
            app.logger.error("[Auth] unexpected error: %s", error)
            return jsonify(error="Không kết nối được Odoo, vui lòng thử lại sau.",
                           code="odoo_error"), 502
        return (jsonify(error=error.message, code=error.code),
                ERROR_STATUS.get(error.code, 502))

    @app.before_request
    def require_login():
        if _is_public(request.path):
            return None
        uid = session.get("uid")
        if uid and clock() - session.get("checked", 0) > RECHECK_SECONDS:
            try:
                user = read_user(uid)
            except Exception as e:  # Odoo down: keep the session, retry next time
                app.logger.warning("[Auth] daily re-check skipped: %s", e)
                user = {"active": True, "share": False}
            if is_internal(user):
                session["checked"] = clock()
            else:
                session.clear()
                uid = None
        if uid:
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="Chưa đăng nhập", code="auth_required"), 401
        return redirect(url_for("auth.login_page", next=request.full_path.rstrip("?")))

    bp = Blueprint("auth", __name__)

    @bp.get("/login")
    def login_page():
        if session.get("uid"):
            return redirect(_safe_next(request.args.get("next")))
        return render_template("login.html")

    @bp.post("/api/auth/login")
    def api_login():
        data = request.get_json(silent=True) or {}
        login = str(data.get("login") or "").strip()
        password = str(data.get("password") or "")
        if not login or not password or len(login) > 200 or len(password) > 500:
            return jsonify(error="Vui lòng nhập email và mật khẩu Odoo.",
                           code="invalid_input"), 400
        if store.too_many_logins(_client_ip(), clock()):
            return jsonify(error="Thử đăng nhập quá nhiều lần, vui lòng chờ 15 phút.",
                           code="too_many_attempts"), 429
        try:
            result = web_login().start_login(login, password)
        except Exception as e:
            return odoo_error(e)
        if not result["mfa_required"]:
            return jsonify(
                error=("Tài khoản này chưa bật xác thực 2 lớp (2FA) trên Odoo. Vào Odoo → "
                       "ảnh đại diện → Hồ sơ của tôi → Bảo mật tài khoản → bật Xác thực "
                       "hai yếu tố, rồi đăng nhập lại."),
                code="totp_not_enabled"), 403
        pid = store.create_pending(result["session_id"], login, clock())
        resp = jsonify(step="totp")
        resp.set_cookie(PENDING_COOKIE, pid, max_age=PENDING_TTL, httponly=True,
                        samesite="Lax", secure=app.config["SESSION_COOKIE_SECURE"])
        return resp

    @bp.post("/api/auth/totp")
    def api_totp():
        pid = request.cookies.get(PENDING_COOKIE, "")
        data = request.get_json(silent=True) or {}
        code = re.sub(r"\s", "", str(data.get("code") or ""))
        if not re.fullmatch(r"\d{6}", code):
            return jsonify(error="Mã xác thực gồm 6 chữ số.", code="invalid_input"), 400
        entry = store.take_attempt(pid, clock()) if pid else None
        if not entry:
            return jsonify(error="Phiên đăng nhập đã hết hạn, vui lòng nhập lại mật khẩu.",
                           code="pending_expired"), 401
        if entry["attempts"] > MAX_TOTP_ATTEMPTS:
            store.drop_pending(pid)
            resp = jsonify(error="Nhập sai mã quá nhiều lần, vui lòng đăng nhập lại.",
                           code="too_many_attempts")
            resp.delete_cookie(PENDING_COOKIE)
            return resp, 429
        try:
            uid = web_login().verify_totp(entry["session_id"], code)
            user = read_user(uid)
        except Exception as e:
            if not (isinstance(e, OdooLoginError) and e.code == "invalid_totp"):
                store.drop_pending(pid)
            return odoo_error(e)
        store.drop_pending(pid)
        if not is_internal(user):
            resp = jsonify(error="Chỉ nhân viên nội bộ mới được dùng hệ thống này.",
                           code="not_internal")
            resp.delete_cookie(PENDING_COOKIE)
            return resp, 403
        session.clear()
        session.permanent = True
        session.update(uid=uid, name=user["name"], login=user["login"], checked=clock())
        app.logger.info("[Auth] login ok uid=%s", uid)
        resp = jsonify(user={"name": user["name"], "login": user["login"]},
                       next=_safe_next(data.get("next")))
        resp.delete_cookie(PENDING_COOKIE)
        return resp

    @bp.get("/api/auth/me")
    def api_me():
        if not session.get("uid"):
            return jsonify(error="Chưa đăng nhập", code="auth_required"), 401
        return jsonify(user={"name": session.get("name"), "login": session.get("login")})

    @bp.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("auth.login_page"))

    app.register_blueprint(bp)

    @app.context_processor
    def inject_user():
        return {"current_user": {"name": session.get("name"), "login": session.get("login")}
                if session.get("uid") else None}


def _safe_next(target) -> str:
    target = str(target or "")
    # Only same-site absolute paths; blocks //evil.com and /\evil.com.
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target
