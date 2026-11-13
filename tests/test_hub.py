"""Offline tests for the CS Hub login gate and the delivery report (no Odoo calls)."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("HUB_SECRET_KEY", "x" * 48)
os.environ["HUB_COOKIE_SECURE"] = "0"

import delivery_report as dr  # noqa: E402
import hub_auth  # noqa: E402

PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)).decode()


# ── pure helpers ────────────────────────────────────────────────────────────

def test_format_qty_uses_vietnamese_separators():
    assert dr.format_qty(2) == "2,00"
    assert dr.format_qty(1234.5) == "1.234,50"


def test_html_to_text_strips_markup_and_keeps_lines():
    assert dr.html_to_text("<p>Giao <b>tầng 3</b></p><p>Gọi trước&nbsp;30'</p>") == "Giao tầng 3\nGọi trước\xa030'"
    assert dr.html_to_text(False) == ""
    assert "<script>" not in dr.html_to_text("<script>alert(1)</script>ok")


def test_item_description_drops_repeated_product_name():
    name = "ORD-SOFA-MILD (2 seater)"
    assert dr.item_description(f"{name}\nVải LIBERTY 2\nChân gỗ", (name, "ORD-SOFA-MILD")) == "Vải LIBERTY 2\nChân gỗ"
    assert dr.item_description("Mô tả riêng\nDòng 2", (name, "x")) == "Mô tả riêng\nDòng 2"


def test_clean_address_drops_empty_lines():
    raw = "CÔNG TY A\n12 Đường Mẫu, Phường Tân Mỹ,\n\n\nThành phố Hồ Chí Minh Vietnam"
    assert dr.clean_address(raw) == "CÔNG TY A\n12 Đường Mẫu, Phường Tân Mỹ\nThành phố Hồ Chí Minh Vietnam"


def test_safe_next_blocks_open_redirect():
    assert hub_auth._safe_next("/bien-ban?q=1") == "/bien-ban?q=1"
    for bad in ("https://evil.com", "//evil.com", "/\\evil.com", "", None):
        assert hub_auth._safe_next(bad) == "/"


def test_pdf_fetcher_blocks_remote_urls():
    for bad in ("http://169.254.169.254/latest", "file:///etc/passwd",
                (dr.FONTS_DIR / ".." / "hub.html").resolve().as_uri()):
        with pytest.raises(ValueError):
            dr.check_pdf_resource(bad)
    dr.check_pdf_resource("data:image/png;base64,AAAA")
    dr.check_pdf_resource((dr.FONTS_DIR / "Montserrat-Regular.ttf").as_uri())


# ── report assembly with a fake Odoo ────────────────────────────────────────

class FakeOdoo:
    def __init__(self, picking_state="done", picking_type="outgoing"):
        self.picking_state = picking_state
        self.picking_type = picking_type
        self.calls = []

    def __call__(self, conn, model, method, *args, **kw):
        self.calls.append((model, method))
        assert method in {"search_read", "read"}, "report must stay read-only"
        if model == "stock.picking":
            domain = args[0]
            if ("picking_type_code", "=", "outgoing") in domain and self.picking_type != "outgoing":
                return []
            return [{"id": 7, "name": "O-MID/OUT/00473", "origin": "O-BG-2610-0649",
                     "sale_id": [5506, "O-BG-2610-0649"], "partner_id": [48317, "KHÁCH HÀNG MẪU"],
                     "user_id": [332, "BON SC, NGUYỄN NGỌC THANH"], "company_id": [11, "Ordinaire Việt Nam"],
                     "note": "<p>Giao giờ hành chính</p>", "state": self.picking_state,
                     "scheduled_date": "2026-11-06 00:00:00", "date_done": "2026-11-06 04:43:08",
                     "picking_type_id": [246, "Kho Trung Gian: Delivery Orders"], "move_ids": [1, 2, 3]}]
        if model == "res.company":
            return [{"id": 11, "logo": PNG_1PX}]
        if model == "res.partner":
            return [{"id": 48317, "name": "CÔNG TY TNHH KHÁCH HÀNG MẪU", "display_name": "CÔNG TY TNHH KHÁCH HÀNG MẪU",
                     "contact_address": "CÔNG TY TNHH KHÁCH HÀNG MẪU\n12 Đường Mẫu,\n\nHCM",
                     "phone": False, "user_id": [50, "ORD Sales, VÕ ĐỖ ANH THƯ"]}]
        if model == "stock.move.line":  # two lines of the same move (armchair) → one row
            return [{"move_id": [3, "m3"]}, {"move_id": [1, "m1"]}, {"move_id": [2, "m2"]}, {"move_id": [2, "m2"]}]
        if model == "stock.move":
            return [
                {"id": 1, "product_id": [101, "ORD-SOFA-MILD"], "product_uom_qty": 1.0, "product_uom": [1, "Cái"], "sale_line_id": [11, "x"]},
                {"id": 2, "product_id": [102, "ORD-ARMCHAIR-MILD"], "product_uom_qty": 2.0, "product_uom": [1, "Cái"], "sale_line_id": [12, "x"]},
                {"id": 3, "product_id": [103, "ORD-COFFEE TABLE-SORA"], "product_uom_qty": 1.0, "product_uom": [1, "Cái"], "sale_line_id": False},
            ]
        if model == "product.product":
            return [{"id": 101, "display_name": "ORD-SOFA-MILD", "name": "ORD-SOFA-MILD", "image_512": PNG_1PX},
                    {"id": 102, "display_name": "ORD-ARMCHAIR-MILD", "name": "ORD-ARMCHAIR-MILD", "image_512": False},
                    {"id": 103, "display_name": "ORD-COFFEE TABLE-SORA", "name": "ORD-COFFEE TABLE-SORA", "image_512": False}]
        if model == "sale.order.line":
            return [{"id": 11, "name": "ORD-SOFA-MILD\nVải LIBERTY 2", "x_studio_fabric_codes": [9, "LIBERTY 2"]},
                    {"id": 12, "name": "ORD-ARMCHAIR-MILD", "x_studio_fabric_codes": False}]
        if model == "x_fabric_codes":
            return [{"id": 9, "display_name": "LIBERTY 2", "x_studio_image": PNG_1PX}]
        raise AssertionError(f"unexpected {model}.{method}")


def test_build_report_mirrors_odoo_template():
    r = dr.build_report(FakeOdoo(), {}, 7)
    assert [it["product"] for it in r["items"]] == ["ORD-COFFEE TABLE-SORA", "ORD-SOFA-MILD", "ORD-ARMCHAIR-MILD"]
    assert [it["index"] for it in r["items"]] == [1, 2, 3]
    assert r["items"][2]["quantity"] == "2,00 Cái"
    assert r["items"][1]["details"] == "Vải LIBERTY 2"
    assert r["items"][1]["fabric_image"].startswith("data:image/png;base64,")
    assert r["has_fabric_codes"] is True
    assert r["sender"]["unit"] == "CÔNG TY TNHH NỘI THẤT ORDINAIRE VIỆT NAM"
    assert r["partner"]["salesperson"] == "ORD Sales, VÕ ĐỖ ANH THƯ"
    assert r["partner"]["contact_address"] == "CÔNG TY TNHH KHÁCH HÀNG MẪU\n12 Đường Mẫu\nHCM"
    assert "responsible" not in r
    assert r["note"] == "Giao giờ hành chính"
    assert r["so_name"] == "O-BG-2610-0649"


def test_build_report_rejects_non_delivery_pickings():
    assert dr.build_report(FakeOdoo(picking_type="incoming"), {}, 7) is None


# ── Flask: login gate + routes ──────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    from flask import Flask
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    fake = FakeOdoo()
    hub_auth.init_auth(app, get_odoo=lambda: {"baseUrl": "https://odoo.invalid", "db": "x"}, rpc=fake)
    app.register_blueprint(dr.create_blueprint(lambda: {}, fake))

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def home():
        return "home"

    return app.test_client()


def login(client, uid=208):
    with client.session_transaction() as s:
        s.update(uid=uid, name="BON BIS, VŨ ĐÌNH DŨNG", login="a@b.vn", checked=10**12)


def test_gate_blocks_anonymous(client):
    assert client.get("/api/health").status_code == 200
    r = client.get("/bien-ban")
    assert r.status_code == 302 and "/login?next=/bien-ban" in r.headers["Location"]
    assert client.get("/api/delivery/search?q=O-BG").status_code == 401
    assert client.get("/api/delivery/7/bien-ban.pdf").status_code == 401
    r = client.get("/zns/dashboard")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert client.get("/login").status_code == 200


def test_search_and_pdf_when_logged_in(client):
    login(client)
    assert client.get("/api/delivery/search?q=ab").status_code == 400
    r = client.get("/api/delivery/search?q=O-BG-2610-0649")
    assert r.status_code == 200
    row = r.get_json()["results"][0]
    assert row["name"] == "O-MID/OUT/00473" and row["state_label"] == "Hoàn tất" and row["printable"]
    pdf = client.get("/api/delivery/7/bien-ban.pdf")
    assert pdf.status_code == 200 and pdf.data[:5] == b"%PDF-"
    assert "inline" in pdf.headers["Content-Disposition"]
    assert "attachment" in client.get("/api/delivery/7/bien-ban.pdf?download=1").headers["Content-Disposition"]


def test_cancelled_picking_is_not_printable(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    from flask import Flask
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    fake = FakeOdoo(picking_state="cancel")
    hub_auth.init_auth(app, get_odoo=lambda: {}, rpc=fake)
    app.register_blueprint(dr.create_blueprint(lambda: {}, fake))
    c = app.test_client()
    login(c)
    assert c.get("/api/delivery/search?q=O-BG").get_json()["results"][0]["printable"] is False
    assert c.get("/api/delivery/7/bien-ban.pdf").status_code == 409


def test_totp_requires_pending_login(client):
    r = client.post("/api/auth/totp", json={"code": "123456"})
    assert r.status_code == 401 and r.get_json()["code"] == "pending_expired"
    assert client.post("/api/auth/totp", json={"code": "12"}).status_code == 400


def test_daily_recheck_logs_out_archived_user(client, monkeypatch):
    with client.session_transaction() as s:
        s.update(uid=99, name="x", login="x", checked=0)
    archived = lambda conn, model, method, *a, **k: [{"id": 99, "name": "x", "login": "x", "active": False, "share": False}]
    # Rebuild app with an rpc that reports the user as archived.
    from flask import Flask
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    hub_auth.init_auth(app, get_odoo=lambda: {}, rpc=archived)

    @app.get("/x")
    def x():
        return "ok"

    c = app.test_client()
    with c.session_transaction() as s:
        s.update(uid=99, name="x", login="x", checked=0)
    assert c.get("/x").status_code == 302


def test_login_flow_with_fake_odoo(client, monkeypatch):
    class FakeWebLogin:
        def __init__(self, *a):
            pass

        def start_login(self, login, password):
            if password != "pw":
                raise hub_auth.OdooLoginError("invalid_credentials", "Sai email hoặc mật khẩu Odoo.")
            return {"mfa_required": True, "session_id": "sid"}

        def verify_totp(self, session_id, code):
            if code != "123456":
                raise hub_auth.OdooLoginError("invalid_totp", "Mã xác thực không đúng.")
            return 208

    monkeypatch.setattr(hub_auth, "OdooWebLogin", FakeWebLogin)
    # read_user goes through the fake rpc → make res.users answer.
    orig = FakeOdoo.__call__

    def call(self, conn, model, method, *a, **k):
        if model == "res.users":
            return [{"id": 208, "name": "BON BIS, VŨ ĐÌNH DŨNG", "login": "a@b.vn", "active": True, "share": False}]
        return orig(self, conn, model, method, *a, **k)

    monkeypatch.setattr(FakeOdoo, "__call__", call)
    assert client.post("/api/auth/login", json={"login": "a@b.vn", "password": "bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"login": "a@b.vn", "password": "pw"}).get_json() == {"step": "totp"}
    assert client.post("/api/auth/totp", json={"code": "000000"}).status_code == 401
    r = client.post("/api/auth/totp", json={"code": "123456", "next": "//evil.com"})
    assert r.status_code == 200 and r.get_json()["next"] == "/"
    assert client.get("/api/auth/me").get_json()["user"]["login"] == "a@b.vn"
    assert client.get("/bien-ban").status_code == 200
    client.get("/logout")
    assert client.get("/bien-ban").status_code == 302


def test_session_slides_while_user_stays_active(tmp_path, monkeypatch):
    import time as time_mod
    now = [1_800_000_000.0]
    monkeypatch.setattr(time_mod, "time", lambda: now[0])
    monkeypatch.setenv("HUB_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    from flask import Flask
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    hub_auth.init_auth(app, get_odoo=lambda: {}, rpc=FakeOdoo())

    @app.get("/x")
    def x():
        return "ok"

    c = app.test_client()
    with c.session_transaction() as s:
        s.update(uid=208, name="x", login="x", checked=now[0], _permanent=True)
    day = 86400
    # Active user: one visit every 80 days for a year never hits the login page.
    for _ in range(5):
        now[0] += 80 * day
        r = c.get("/x")
        assert r.status_code == 200 and "hub_session=" in r.headers.get("Set-Cookie", "")
    # Only a full SESSION_DAYS without any visit ends the session.
    now[0] += (hub_auth.SESSION_DAYS + 1) * day
    assert c.get("/x").status_code == 302


def _zns_app(tmp_path, monkeypatch, fetch):
    import zns_proxy
    monkeypatch.setenv("HUB_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    from flask import Flask
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    hub_auth.init_auth(app, get_odoo=lambda: {}, rpc=FakeOdoo())
    app.register_blueprint(zns_proxy.create_blueprint(fetch))
    c = app.test_client()
    login(c)
    return c


def test_zns_dashboard_is_served_without_second_login(tmp_path, monkeypatch):
    monkeypatch.setenv("ZNS_ADMIN_API_KEY", "k" * 40)
    seen = {}

    def fetch(query):
        seen.update(query)
        return 200, "<html><body class='x'><h1>ZNS Dashboard</h1></body></html>".encode()

    c = _zns_app(tmp_path, monkeypatch, fetch)
    r = c.get("/zns/dashboard?from_date=2026-11-01&template_type=rating&evil=1")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "ZNS Dashboard" in page and "← CS Hub" in page
    assert page.index("<body class='x'>") < page.index("← CS Hub")
    assert seen == {"from_date": "2026-11-01", "template_type": "rating"}
    assert "k" * 40 not in page and r.headers["Cache-Control"] == "no-store"


def test_zns_dashboard_upstream_failure_and_missing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ZNS_ADMIN_API_KEY", "k" * 40)
    c = _zns_app(tmp_path, monkeypatch, lambda q: (401, b""))
    assert c.get("/zns/dashboard").status_code == 502
    monkeypatch.delenv("ZNS_ADMIN_API_KEY")
    assert c.get("/zns/dashboard").status_code == 503


def test_fetch_dashboard_sends_basic_auth(monkeypatch):
    import zns_proxy
    monkeypatch.setenv("ZNS_ADMIN_API_KEY", "secret-key")
    monkeypatch.setenv("ZNS_UPSTREAM_URL", "https://zns.invalid/")
    captured = {}

    class Resp:
        status = 200
        def read(self): return b"ok"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        captured["url"], captured["auth"] = req.full_url, req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(zns_proxy.urllib.request, "urlopen", fake_urlopen)
    assert zns_proxy.fetch_dashboard({"to_date": "2026-11-13"}) == (200, b"ok")
    assert captured["url"] == "https://zns.invalid/zns/dashboard?to_date=2026-11-13"
    import base64
    assert base64.b64decode(captured["auth"].split()[1]).decode() == "cs-hub:secret-key"


def test_sender_signature_is_left_blank():
    html = (ROOT / "templates" / "delivery_report.html").read_text()
    sign = html[html.index("ĐẠI DIỆN BÊN GIAO"):html.index("ĐẠI DIỆN BÊN NHẬN")]
    assert "{{" not in sign
