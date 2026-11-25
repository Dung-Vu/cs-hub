"""Offline tests for Báo giá → Biên bản nghiệm thu (synthetic quote, no customer data)."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("HUB_SECRET_KEY", "x" * 48)
os.environ["HUB_COOKIE_SECURE"] = "0"

pytest.importorskip("fitz")

import hub_auth  # noqa: E402
import quote_bbnt as qb  # noqa: E402
from quote_fixture import synthetic_quote_pdf  # noqa: E402
from test_hub import FakeOdoo, login  # noqa: E402


@pytest.fixture(scope="module")
def quote_pdf() -> bytes:
    return synthetic_quote_pdf()


def _has_url_fetcher() -> bool:
    try:
        from weasyprint.urls import URLFetcher  # noqa: F401
    except ImportError:
        return False
    return True


needs_fetcher = pytest.mark.skipif(not _has_url_fetcher(), reason="weasyprint without URLFetcher (<68)")


# ── helpers ────────────────────────────────────────────────────────────────

def test_vn_number_swaps_separators_only_for_numbers():
    assert qb.vn_number("3.00") == "3,00"
    assert qb.vn_number("2,059,840") == "2.059.840"
    assert qb.vn_number("1,234.50") == "1.234,50"
    assert qb.vn_number("12") == "12"
    assert qb.vn_number("1 tấm/cửa") == "1 tấm/cửa"
    assert qb.vn_number("v1.2.3") == "v1.2.3"


def test_output_name_strips_quotation_words():
    assert qb.output_name("Bonario Quotation - S01234.pdf") == "BBNT - S01234.pdf"
    assert qb.output_name("Báo giá Rèm phòng ngủ.PDF") == "BBNT - Rèm phòng ngủ.pdf"
    assert qb.output_name("../../x<>.pdf") == "BBNT - x.pdf"
    assert qb.output_name("") == "BBNT.pdf"


# ── parser ─────────────────────────────────────────────────────────────────

def test_parse_drops_prices_and_adds_check(quote_pdf):
    q = qb.parse_quote(quote_pdf)
    assert q["kind"] == "bonario" and q["category"] == "Rèm cửa"
    assert q["info"] == {"customer": "Anh Khách Mẫu", "address": "1 Đường Giả Định", "phone": "0900 000 000"}
    assert q["company_lines"][0] == "CÔNG TY MẪU NỘI THẤT" and q["logo"].startswith("data:image/png")
    assert q["company"]["vat"] == qb.COMPANIES["bonario"]["vat"]

    (sec,) = q["sections"]
    assert sec.title == "PHÒNG NGỦ"
    assert [(h.text, h.colspan, h.rowspan) for h in sec.header] == [
        ("Khu Vực", 1, 2), ("Vị trí", 1, 2), ("Mã Sản Phẩm", 1, 2), ("Thông số kích thước", 2, 1),
        ("Khối lượng (m2)", 1, 2), ("Check", 1, 2), ("Ghi chú", 1, 2)]
    assert sec.sub_header == ["Ngang (m)", "Cao (m)"]
    assert len(sec.widths) == 8 and abs(sum(sec.widths) - 100) < 0.1

    assert len(sec.rows) == 2  # "Tổng cộng" dropped
    first, second = ([c.text for c in r] for r in sec.rows)
    assert first == ["CỬA SỔ", "1", "MAU-01", "3,00", "1,60", "4,80", "", ""]
    assert second == ["CỬA ĐI", "2", "MAU-02", "1,20", "2,50", "3,00", "", "Treo cao"]
    assert sec.rows[0][2].image.startswith("data:image/png") and sec.rows[1][2].image is None
    flat = " ".join(c.text for r in sec.rows for c in r)
    assert "429" not in flat and "2.059.840" not in flat


@pytest.mark.parametrize("data, msg", [
    (b"not a pdf", "PDF"),
])
def test_parse_rejects_garbage(data, msg):
    with pytest.raises(qb.QuoteError, match=msg):
        qb.parse_quote(data)


def test_parse_requires_title_and_table():
    from weasyprint import HTML
    with pytest.raises(qb.QuoteError, match="BÁO GIÁ"):
        qb.parse_quote(HTML(string="<p>Hoá đơn</p>").write_pdf())
    with pytest.raises(qb.QuoteError, match="Khu vực"):
        qb.parse_quote(HTML(string="<h1>BẢNG BÁO GIÁ</h1><p>Khách hàng: A</p>").write_pdf())


@needs_fetcher
def test_render_pdf_is_a_single_page(quote_pdf):
    import fitz
    from flask import Flask
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    with app.app_context():
        pdf = qb.render_pdf(qb.parse_quote(quote_pdf))
    doc = fitz.open(stream=pdf, filetype="pdf")
    text = doc[0].get_text()
    assert "BIÊN BẢN BÀN GIAO VÀ NGHIỆM THU" in text and "Check" in text and "Đơn giá" not in text
    assert "Anh Khách Mẫu" in text and qb.COMPANIES["bonario"]["legal_name"] in text


# ── routes ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    from flask import Flask
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    hub_auth.init_auth(app, get_odoo=lambda: {"baseUrl": "https://odoo.invalid", "db": "x"}, rpc=FakeOdoo())
    app.register_blueprint(qb.create_blueprint())
    return app.test_client()


def _post(client, data: bytes, name="Bonario Quotation - S0001.pdf"):
    return client.post("/api/bbnt.pdf", data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def test_bbnt_requires_login(client, quote_pdf):
    r = client.get("/bbnt")
    assert r.status_code == 302 and "/login?next=/bbnt" in r.headers["Location"]
    assert _post(client, quote_pdf).status_code == 401


def test_bbnt_page_and_errors(client, monkeypatch):
    login(client)
    assert "Tạo biên bản" in client.get("/bbnt").get_data(as_text=True)
    r = client.post("/api/bbnt.pdf", data={}, content_type="multipart/form-data")
    assert r.status_code == 400 and r.json["code"] == "no_file"
    r = _post(client, b"PK\x03\x04 excel", "bao-gia.xlsx")
    assert r.status_code == 400 and r.json["code"] == "not_pdf"
    r = _post(client, b"%PDF-1.7 broken")
    assert r.status_code == 422 and r.json["code"] == "unreadable"
    monkeypatch.setattr(qb, "MAX_UPLOAD", 10)
    r = _post(client, b"%PDF-" + b"x" * 64)
    assert r.status_code == 413 and r.json["code"] == "too_large"


@needs_fetcher
def test_bbnt_converts_quote(client, quote_pdf):
    login(client)
    r = _post(client, quote_pdf)
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert r.data.startswith(b"%PDF")
    assert r.headers["X-Filename"] == "BBNT%20-%20S0001.pdf"
    assert "inline" in r.headers["Content-Disposition"]
