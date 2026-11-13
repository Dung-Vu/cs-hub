"""zns_proxy.py — shows the ZNS dashboard (auto-workflow) inside the CS Hub.

The ZNS dashboard is protected by the ZNS admin key. The hub is already behind
Odoo login + 2FA, so it fetches the dashboard server-side with that key and
serves it at the same path (/zns/dashboard): CS users never see the key and
never get a second login prompt. The upstream page only uses relative links
({{ request.path }}), so its filter form keeps working through the hub.
"""

from __future__ import annotations

import base64
import os
import urllib.error
import urllib.parse
import urllib.request

from flask import Blueprint, Response, redirect, request

ALLOWED_PARAMS = ("from_date", "to_date", "template_type")
TIMEOUT = 30

_BACK_BAR = (
    '<div style="font:600 13px/1.4 system-ui,sans-serif;padding:10px 16px;'
    'background:#0d6b62;color:#fff">'
    '<a href="/" style="color:#fff;text-decoration:none">← CS Hub</a>'
    '<span style="opacity:.75"> · Dashboard ZNS</span></div>'
)


def _error_page(message: str, status: int) -> Response:
    body = (
        '<!DOCTYPE html><html lang="vi"><meta charset="UTF-8"><title>Dashboard ZNS</title>'
        f'<body style="font-family:system-ui,sans-serif;margin:0">{_BACK_BAR}'
        f'<p style="padding:24px">{message}</p></body></html>'
    )
    return Response(body, status, mimetype="text/html")


def fetch_dashboard(query: dict[str, str]) -> tuple[int, bytes]:
    """Fetches the upstream dashboard HTML; returns (status, body)."""
    upstream = os.environ.get("ZNS_UPSTREAM_URL", "https://workflow.bonstu.site").rstrip("/")
    key = os.environ.get("ZNS_ADMIN_API_KEY", "")
    url = f"{upstream}/zns/dashboard"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    token = base64.b64encode(f"cs-hub:{key}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}", "User-Agent": "cs-hub/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def create_blueprint(fetch=fetch_dashboard) -> Blueprint:
    bp = Blueprint("zns_proxy", __name__)

    @bp.get("/zns")
    def zns_root():
        return redirect("/zns/dashboard")

    @bp.get("/zns/dashboard")
    def zns_dashboard():
        if not os.environ.get("ZNS_ADMIN_API_KEY"):
            return _error_page("Dashboard ZNS chưa được cấu hình trên CS Hub. Vui lòng báo team BIS.", 503)
        query = {k: request.args[k].strip()[:40] for k in ALLOWED_PARAMS
                 if request.args.get(k, "").strip()}
        try:
            status, body = fetch(query)
        except Exception:  # network / timeout
            status, body = 0, b""
        if status != 200:
            return _error_page("Không tải được Dashboard ZNS, vui lòng thử lại sau ít phút.", 502)
        page = body.decode("utf-8", errors="replace")
        idx = page.find("<body")
        if idx != -1:
            end = page.find(">", idx) + 1
            page = page[:end] + _BACK_BAR + page[end:]
        resp = Response(page, 200, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return bp
