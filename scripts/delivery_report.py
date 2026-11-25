"""delivery_report.py — "In biên bản bàn giao & nghiệm thu" for CS.

CS has no access to delivery orders in Odoo, so the hub reads them with its own
service account and re-renders the Odoo report
"Biên bản bàn giao & Nghiệm thu" (ir.actions.report 1487, Studio view 5984)
as a PDF. Read-only: nothing is written to Odoo.

Keep the layout in templates/delivery_report.html in sync with that Studio view.
"""

from __future__ import annotations

import base64
import html
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from flask import Blueprint, abort, jsonify, render_template, request, send_file
from io import BytesIO

VN_TZ = timezone(timedelta(hours=7))
CTX = {"lang": "vi_VN", "tz": "Asia/Ho_Chi_Minh", "active_test": False}
ORDINAIRE_COMPANY_ID = 11
MIN_QUERY = 3
MAX_RESULTS = 50

STATE_LABELS = {
    "draft": "Nháp",
    "waiting": "Chờ hoạt động khác",
    "confirmed": "Đang chờ",
    "assigned": "Sẵn sàng",
    "done": "Hoàn tất",
    "cancel": "Đã huỷ",
}

FONTS_DIR = Path(__file__).resolve().parent.parent / "templates" / "fonts"


def _m2o_name(value) -> str:
    return value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else ""


def _m2o_id(value):
    return value[0] if isinstance(value, (list, tuple)) and value else None


def _to_vn(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(VN_TZ).strftime("%d/%m/%Y %H:%M")


def _data_uri(b64: str | bool | None) -> str | None:
    if not b64:
        return None
    raw = base64.b64decode(b64)
    if raw[:4] == b"\x89PNG":
        mime = "image/png"
    elif raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    elif raw.lstrip()[:4] in (b"<svg", b"<?xm"):
        mime = "image/svg+xml"
    else:
        mime = "image/png"
    return f"data:{mime};base64,{b64}"


def html_to_text(value: str | bool | None) -> str:
    """Odoo HTML field → plain text with line breaks (no markup reaches the PDF)."""
    if not value:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", str(value))
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def format_qty(qty: float) -> str:
    """Odoo vi_VN formatting with the 'Product Unit' precision (2): 1.234,50."""
    s = f"{qty:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def clean_address(value: str | bool | None) -> str:
    lines = [ln.strip().rstrip(",").strip() for ln in str(value or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def item_description(sale_line_name: str, product_names: tuple[str, ...]) -> str:
    """Studio template: drop the first line of the SO line description when it
    repeats the product name, keep the rest."""
    lines = (sale_line_name or "").split("\n")
    if lines and lines[0].strip() in product_names:
        lines = lines[1:]
    return "\n".join(lines).strip()


# ── Odoo reads ──────────────────────────────────────────────────────────────

def search_pickings(rpc, conn, query: str) -> list[dict]:
    """Customer delivery orders whose own code, source document or SO match."""
    domain = [
        ("picking_type_code", "=", "outgoing"),
        "|", "|",
        ("name", "ilike", query),
        ("origin", "ilike", query),
        ("sale_id.name", "ilike", query),
    ]
    rows = rpc(conn, "stock.picking", "search_read", domain,
               fields=["name", "origin", "sale_id", "partner_id", "company_id", "state",
                       "scheduled_date", "date_done", "picking_type_id", "move_ids"],
               limit=MAX_RESULTS, order="scheduled_date desc, id desc", context=CTX)
    return [{
        "id": r["id"],
        "name": r["name"],
        "sale_order": _m2o_name(r["sale_id"]) or (r["origin"] or ""),
        "customer": _m2o_name(r["partner_id"]),
        "company": _m2o_name(r["company_id"]),
        "operation": _m2o_name(r["picking_type_id"]),
        "state": r["state"],
        "state_label": STATE_LABELS.get(r["state"], r["state"]),
        "scheduled_date": _to_vn(r["scheduled_date"]),
        "date_done": _to_vn(r["date_done"]),
        "item_count": len(r["move_ids"] or []),
        "printable": r["state"] != "cancel",
    } for r in rows]


def build_report(rpc, conn, picking_id: int) -> dict | None:
    """Everything templates/delivery_report.html needs for one delivery order."""
    found = rpc(conn, "stock.picking", "search_read",
                [("id", "=", picking_id), ("picking_type_code", "=", "outgoing")],
                fields=["name", "origin", "sale_id", "partner_id", "user_id",
                        "company_id", "note", "state"], context=CTX)
    if not found:
        return None
    p = found[0]

    company_id = _m2o_id(p["company_id"])
    logo = None
    if company_id:
        logo = _data_uri(rpc(conn, "res.company", "read", [company_id],
                             fields=["logo"], context=CTX)[0].get("logo"))

    partner = {"display_name": "", "contact_address": "", "phone": "",
               "salesperson": "", "name": ""}
    partner_id = _m2o_id(p["partner_id"])
    if partner_id:
        pr = rpc(conn, "res.partner", "read", [partner_id],
                 fields=["name", "display_name", "contact_address", "phone", "user_id"],
                 context=CTX)[0]
        partner = {
            "name": pr.get("name") or "",
            "display_name": pr.get("display_name") or "",
            "contact_address": clean_address(pr.get("contact_address")),
            "phone": pr.get("phone") or "",
            "salesperson": _m2o_name(pr.get("user_id")),
        }

    # Studio view iterates move lines (model default order) and keeps one row per move.
    move_lines = rpc(conn, "stock.move.line", "search_read",
                     [("picking_id", "=", picking_id)],
                     fields=["move_id", "product_id"], context=CTX)
    move_ids: list[int] = []
    for ml in move_lines:
        mid = _m2o_id(ml["move_id"])
        if mid and mid not in move_ids:
            move_ids.append(mid)

    moves = {m["id"]: m for m in rpc(conn, "stock.move", "read", move_ids,
                                     fields=["product_id", "product_uom_qty",
                                             "product_uom", "sale_line_id"],
                                     context=CTX)} if move_ids else {}

    product_ids = sorted({_m2o_id(m["product_id"]) for m in moves.values()} - {None})
    products = {pr["id"]: pr for pr in rpc(conn, "product.product", "read", product_ids,
                                           fields=["display_name", "name", "image_512"],
                                           context=CTX)} if product_ids else {}

    sale_line_ids = sorted({_m2o_id(m["sale_line_id"]) for m in moves.values()} - {None})
    sale_lines = {sl["id"]: sl for sl in rpc(conn, "sale.order.line", "read", sale_line_ids,
                                             fields=["name", "x_studio_fabric_codes"],
                                             context=CTX)} if sale_line_ids else {}

    fabric_ids = sorted({_m2o_id(sl["x_studio_fabric_codes"])
                         for sl in sale_lines.values()} - {None})
    fabrics = {f["id"]: f for f in rpc(conn, "x_fabric_codes", "read", fabric_ids,
                                       fields=["display_name", "x_studio_image"],
                                       context=CTX)} if fabric_ids else {}

    note = html_to_text(p.get("note"))
    items = []
    for idx, mid in enumerate(move_ids, start=1):
        m = moves[mid]
        product = products.get(_m2o_id(m["product_id"]), {})
        sale_line = sale_lines.get(_m2o_id(m["sale_line_id"]), {})
        fabric = fabrics.get(_m2o_id(sale_line.get("x_studio_fabric_codes")), {})
        product_name = product.get("display_name") or _m2o_name(m["product_id"])
        items.append({
            "index": idx,
            "product_id": _m2o_id(m["product_id"]),
            "sale_line_name": sale_line.get("name") or "",
            "uom": _m2o_name(m["product_uom"]),
            "qty": format_qty(m["product_uom_qty"] or 0),
            "product": product_name,
            "image": _data_uri(product.get("image_512")),
            "details": item_description(sale_line.get("name") or "",
                                        (product_name, product.get("name") or "")),
            "fabric_code": fabric.get("display_name") or "",
            "fabric_image": _data_uri(fabric.get("x_studio_image")),
            "quantity": f"{format_qty(m['product_uom_qty'] or 0)} {_m2o_name(m['product_uom'])}".strip(),
            "note": note,
        })

    is_ordinaire = company_id == ORDINAIRE_COMPANY_ID
    return {
        "picking_name": p["name"],
        "state": p["state"],
        "print_date": datetime.now(VN_TZ).strftime("%d/%m/%Y"),
        "so_name": _m2o_name(p["sale_id"]) or (p["origin"] or ""),
        "logo": logo,
        "company_name": _m2o_name(p["company_id"]),
        "sender": {
            "unit": ("CÔNG TY TNHH NỘI THẤT ORDINAIRE VIỆT NAM" if is_ordinaire
                     else "CÔNG TY TNHH XNK BONARIO VIỆT NAM"),
            "address": ("10 Bac Nam 2 Street, 4.8-ha Residential Area, Binh Trung Ward, Ho Chi Minh City"
                        if is_ordinaire
                        else "Showroom 22 đường 19A, P. An Phú, TP. Thủ Đức, TP. HCM"),
            "phone": "028 666 09009",
        },
        "partner": partner,
        "partner_id": partner_id,
        "is_ordinaire": is_ordinaire,
        "note": note,
        "has_fabric_codes": any(it["fabric_code"] for it in items),
        "items": items,
    }


# ── Biên bản nghiệm thu — vật liệu dán tường (mẫu giấy cũ) ──────────────────

_NUM = r"(\d+(?:[.,]\d+)?)"
_SPEC_RE = re.compile(_NUM + r"\s*m?\s*[xX×*]\s*" + _NUM + r"\s*m\b")
_REPEAT_RE = re.compile(r"(?i)(?:lặp|repeat)[^\d\n]{0,20}" + _NUM + r"\s*(cm|mm|m)?")


def _num(value: str) -> float:
    return float(value.replace(",", "."))


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def roll_spec(*texts: str) -> dict:
    """'Quy cách: 0.7 x 10 m' → rộng khổ / dài cuộn / diện tích cuộn (+ độ lặp hoa văn
    when written in the text). Empty strings when nothing is written in Odoo."""
    spec = {"width": "", "length": "", "area": "", "repeat": ""}
    for text in texts:
        text = text or ""
        if not spec["width"] and (m := _SPEC_RE.search(text)):
            w, ln = _num(m.group(1)), _num(m.group(2))
            spec.update(width=f"{_fmt(w)} m", length=f"{_fmt(ln)} m", area=f"{_fmt(w * ln)} m²")
        if not spec["repeat"] and (m := _REPEAT_RE.search(text)):
            spec["repeat"] = f"{m.group(1)} {m.group(2) or 'cm'}"
    return spec


def vendor_name(value: str | bool | None) -> str:
    """'SANGETSU GOODRICH (0.0 m - 0 ₫)' → 'SANGETSU GOODRICH'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(value or "")).strip()


def build_acceptance(rpc, conn, picking_id: int) -> dict | None:
    """Data for templates/acceptance_wallcovering.html (old paper form
    "BIÊN BẢN NGHIỆM THU — Hạng mục: VẬT LIỆU DÁN TƯỜNG"). Columns Odoo does not
    store (khu vực, vị trí, kích thước tường, check) stay blank for the site team."""
    r = build_report(rpc, conn, picking_id)
    if r is None:
        return None
    pk = rpc(conn, "stock.picking", "read", [picking_id],
             fields=["sale_id", "x_studio_a_ch_ca_th_lin_h"], context=CTX)[0]
    so = {}
    so_id = _m2o_id(pk.get("sale_id"))
    if so_id:
        so = rpc(conn, "sale.order", "read", [so_id],
                 fields=["origin", "user_id", "x_studio_phone"], context=CTX)[0]

    product_ids = sorted({it["product_id"] for it in r["items"]} - {None})
    products = {p["id"]: p for p in rpc(
        conn, "product.product", "read", product_ids,
        fields=["default_code", "description_sale", "x_studio_related_field_508_1iv5212o0"],
        context=CTX)} if product_ids else {}

    for it in r["items"]:
        pr = products.get(it["product_id"], {})
        it["code"] = pr.get("default_code") or it["product"]
        it["collection"] = vendor_name(pr.get("x_studio_related_field_508_1iv5212o0"))
        it["roll"] = roll_spec(pr.get("description_sale") or "", it["sale_line_name"])

    now = datetime.now(VN_TZ)
    r.update(
        project=so.get("origin") or "",
        site_address=clean_address(pk.get("x_studio_a_ch_ca_th_lin_h")) or r["partner"]["contact_address"],
        site_phone=so.get("x_studio_phone") or r["partner"]["phone"],
        salesperson=_m2o_name(so.get("user_id")) or r["partner"]["salesperson"],
        day=now.strftime("%d"), month=now.strftime("%m"), year=now.strftime("%Y"),
        header=(ORDINAIRE_HEADER if r["is_ordinaire"] else BONARIO_HEADER),
    )
    return r


BONARIO_HEADER = {
    "company": "CÔNG TY TNHH XNK BONARIO VIỆT NAM",
    "lines": ["Showroom: 22 đường 19A, P. An Phú, TP. Thủ Đức, TP. HCM",
              "Văn phòng: 669D Đỗ Xuân Hợp, P. Phước Long B, TP. Thủ Đức, TP. HCM",
              "Hotline: 028 666 09009 — Website: bonario.vn"],
}
ORDINAIRE_HEADER = {
    "company": "CÔNG TY TNHH NỘI THẤT ORDINAIRE VIỆT NAM",
    "lines": ["10 Bac Nam 2 Street, 4.8-ha Residential Area, Binh Trung Ward, Ho Chi Minh City",
              "Hotline: 028 666 09009"],
}

TEMPLATES = {
    # ?mau=… → (builder, template, file name prefix)
    "ban-giao": (build_report, "delivery_report.html", "Bien-ban-ban-giao"),
    "dan-tuong": (build_acceptance, "acceptance_wallcovering.html", "Bien-ban-nghiem-thu-dan-tuong"),
}


# ── PDF ─────────────────────────────────────────────────────────────────────

def check_pdf_resource(url: str) -> None:
    """Only inline images and the bundled fonts may reach the PDF renderer
    (Odoo data must never make the server fetch remote or local files)."""
    if url.startswith("data:"):
        return
    if url.startswith("file://"):
        path = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).resolve()
        if path.parent == FONTS_DIR.resolve() and path.suffix == ".ttf":
            return
    raise ValueError(f"Blocked resource: {url[:80]}")


def render_pdf(report: dict, template: str = "delivery_report.html") -> bytes:
    from weasyprint import HTML  # lazy: ~150ms import
    from weasyprint.urls import URLFetcher

    class LocalOnlyFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            check_pdf_resource(url)
            return super().fetch(url, headers)

    page = render_template(template, r=report,
                           fonts_url=FONTS_DIR.resolve().as_uri())
    return HTML(string=page, base_url=FONTS_DIR.resolve().as_uri() + "/",
                url_fetcher=LocalOnlyFetcher(allowed_protocols={"data", "file"},
                                             fail_on_errors=False)).write_pdf()


# ── Routes ──────────────────────────────────────────────────────────────────

def create_blueprint(get_odoo: Callable[[], dict], rpc) -> Blueprint:
    bp = Blueprint("delivery", __name__)

    @bp.get("/bien-ban")
    def delivery_page():
        return render_template("delivery.html")

    @bp.get("/api/delivery/search")
    def api_search():
        query = re.sub(r"\s+", " ", request.args.get("q", "")).strip()
        if len(query) < MIN_QUERY:
            return jsonify(error=f"Nhập ít nhất {MIN_QUERY} ký tự của mã đơn hoặc mã phiếu.",
                           code="query_too_short"), 400
        if len(query) > 100:
            return jsonify(error="Mã quá dài.", code="invalid_input"), 400
        try:
            results = search_pickings(rpc, get_odoo(), query)
        except Exception as e:
            bp_logger().error("[Delivery] search failed: %s", e)
            return jsonify(error="Không tải được dữ liệu từ Odoo, vui lòng thử lại.",
                           code="odoo_error"), 502
        return jsonify(query=query, results=results, limit=MAX_RESULTS)

    @bp.get("/api/delivery/<int:picking_id>/bien-ban.pdf")
    def api_report_pdf(picking_id: int):
        mau = request.args.get("mau", "ban-giao")
        if mau not in TEMPLATES:
            return jsonify(error="Mẫu biên bản không hợp lệ.", code="invalid_input"), 400
        builder, template, prefix = TEMPLATES[mau]
        try:
            report = builder(rpc, get_odoo(), picking_id)
        except Exception as e:
            bp_logger().error("[Delivery] report %s failed: %s", picking_id, e)
            return jsonify(error="Không tải được dữ liệu phiếu từ Odoo, vui lòng thử lại.",
                           code="odoo_error"), 502
        if report is None:
            abort(404)
        if report["state"] == "cancel":
            return jsonify(error="Phiếu đã huỷ, không in biên bản.", code="cancelled"), 409
        pdf = render_pdf(report, template)
        filename = f"{prefix}_{report['picking_name'].replace('/', '-')}.pdf"
        return send_file(BytesIO(pdf), mimetype="application/pdf",
                         as_attachment=request.args.get("download") == "1",
                         download_name=filename)

    return bp


def bp_logger():
    from flask import current_app
    return current_app.logger
