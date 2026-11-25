"""quote_bbnt.py — turn a Sales quotation PDF into a "Biên bản bàn giao và
nghiệm thu" (BBNT).

Sales build quotations in Excel and print them to PDF. The BBNT is the same
document with the money removed: same header, same item table (khu vực, vị trí,
sơ đồ/hình ảnh, giải pháp, mã sản phẩm, thông số kích thước…), the price columns
and totals dropped, a "Check" column added, and the Bên A block + acceptance
text + signatures instead of the payment terms.

The PDF is parsed with PyMuPDF: tables come from the ruling lines, pictures are
re-rendered from the exact cell area (whatever way Excel embedded them), so
every quotation category (rèm, giấy dán tường, nội thất…) works. Both layouts
are read: the Bonario Excel quotation ("BẢNG BÁO GIÁ", Khu vực/Vị trí table) and
the Ordinaire order printed from Odoo ("ĐƠN HÀNG / Sales Order", bilingual
column labels) — the item table is found by its column labels, not a fixed form.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

VN_TZ = timezone(timedelta(hours=7))
MAX_PAGES = 30
IMAGE_DPI = 200

# Bên A — legal details printed on the BBNT (from the BBNT Sales already use and
# the company record in Odoo).
COMPANIES = {
    "bonario": {
        "legal_name": "CÔNG TY TNHH XUẤT NHẬP KHẨU BONARIO VIỆT NAM",
        "address": "669D Đỗ Xuân Hợp, Phường Phước Long B, TP Thủ Đức, TP Hồ Chí Minh",
        "phone": "(028) 666 09009",
        "vat": "0316 100 547",
    },
    "ordinaire": {
        "legal_name": "CÔNG TY TNHH NỘI THẤT ORDINAIRE VIETNAM",
        "address": "10 đường Bắc Nam 2, Khu nhà ở 4, 8 ha, Phường Bình Trưng, TP Hồ Chí Minh",
        "phone": "0876 001 166",
        "vat": "0319 227 968",
    },
}

# Money columns removed from the BBNT (matched on the normalised header label,
# Vietnamese or English). "VAT" is dropped but "Vật liệu" (also "vat lieu") stays.
DROP_COLUMN = re.compile(
    r"^(don gia|thanh tien|tong tien|tong cong|gia ban|gia niem yet|gia goc|chiet khau|"
    r"% ?giam gia|giam gia|so tien|thue|\(?vat\b(?! ?lieu))|unit price|discount|amount|"
    r"\(total\)|\bprice\b")

# Labels that mark the item table's header row (any 3 of them).
HEADER_WORDS = ("khu vuc", "vi tri", "ma san pham", "ma sp", "ten san pham", "san pham",
                "hang muc", "hinh anh", "so luong", "sl", "don vi", "dvt", "don gia",
                "thanh tien", "ghi chu", "stt", "so thu tu", "chi tiet", "mo ta", "quy cach",
                "kich thuoc", "thong so", "bien the", "chat lieu", "giai phap", "item",
                "image", "quantity", "description", "note", "no.")
TITLE_WORDS = ("bang bao gia", "bao gia", "don dat hang", "don hang", "phieu bao gia",
               "sales order", "quotation")
TOTAL_ROW = re.compile(r"^(tong|chiet khau|vat|thue|tam ung|thanh toan|da coc|con lai|giam gia)")


class QuoteError(ValueError):
    """The uploaded file is not a quotation this tool can read."""


def norm(text: str | None) -> str:
    """Lower-case ASCII form for matching labels ("Đơn giá" → "don gia")."""
    text = unicodedata.normalize("NFD", (text or "").replace("đ", "d").replace("Đ", "D"))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text).strip().lower()


def clean(text: str | None) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in (text or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


@dataclass
class Cell:
    text: str = ""
    image: str | None = None  # data URI
    colspan: int = 1
    rowspan: int = 1


@dataclass
class Section:
    title: str = ""
    group_row: list = field(default_factory=list)   # [(label, colspan)] or []
    header: list = field(default_factory=list)      # [Cell] (rowspan 2 when grouped)
    sub_header: list = field(default_factory=list)  # [label] under the groups
    rows: list = field(default_factory=list)        # [[Cell]]
    ncols: int = 0
    widths: list = field(default_factory=list)      # % per kept column
    labels: tuple = ()                              # normalised header labels (repeat detection)


# ── helpers ────────────────────────────────────────────────────────────────

def _png_uri(page, rect) -> str:
    import fitz
    pix = page.get_pixmap(clip=fitz.Rect(rect), dpi=IMAGE_DPI, alpha=False)
    return "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()


def _overlap(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(w, 0) * max(h, 0)


def _cell_image(page, bbox, images) -> str | None:
    """Render the pictures lying inside a cell (union of their boxes)."""
    hits = [im for im in images if _overlap(im, bbox) > 0.5 * (im[2] - im[0]) * (im[3] - im[1])]
    if not hits:
        return None
    x0 = max(min(h[0] for h in hits), bbox[0]); y0 = max(min(h[1] for h in hits), bbox[1])
    x1 = min(max(h[2] for h in hits), bbox[2]); y1 = min(max(h[3] for h in hits), bbox[3])
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return _png_uri(page, (x0, y0, x1, y1))


_WORDS: dict = {}


def _cell_text(page, bbox) -> str:
    """Words whose centre lies in the cell. Excel lets wrapped labels spill over
    the ruling lines, so clipping by box would put one word in two cells."""
    key = (id(page.parent), page.number)
    if key not in _WORDS:
        _WORDS.clear()
        _WORDS[key] = page.get_text("words")
    lines: dict = {}
    for x0, y0, x1, y1, word, block, line, _ in _WORDS[key]:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if bbox[0] <= cx < bbox[2] and bbox[1] <= cy < bbox[3]:
            lines.setdefault((block, line), []).append((x0, y0, word))
    ordered = sorted(lines.values(), key=lambda ws: (round(min(w[1] for w in ws)), min(w[0] for w in ws)))
    return clean("\n".join(" ".join(w[2] for w in sorted(ws)) for ws in ordered))


_NUMBER = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+\.\d+$")


def vn_number(text: str) -> str:
    """Excel prints 1,234.50 — Vietnamese documents use 1.234,50."""
    if _NUMBER.match(text or ""):
        return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return text


def _boundaries(table):
    """Column x-edges and row y-edges of a PyMuPDF table."""
    xs = sorted({round(c[0], 1) for c in table.cells} | {round(c[2], 1) for c in table.cells})
    ys = sorted({round(c[1], 1) for c in table.cells} | {round(c[3], 1) for c in table.cells})
    return _merge_close(xs), _merge_close(ys)


def _merge_close(values, tol=1.5):
    out = []
    for v in values:
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def _index(edges, v, tol=1.5):
    for i, e in enumerate(edges):
        if abs(e - v) <= tol:
            return i
    return min(range(len(edges)), key=lambda i: abs(edges[i] - v))


def _grid(table):
    """[(r0, c0, r1, c1, bbox)] for every real cell, spans included."""
    xs, ys = _boundaries(table)
    out = []
    for bbox in table.cells:
        if bbox is None:
            continue
        c0, c1 = _index(xs, bbox[0]), _index(xs, bbox[2])
        r0, r1 = _index(ys, bbox[1]), _index(ys, bbox[3])
        if c1 > c0 and r1 > r0:
            out.append((r0, c0, r1, c1, tuple(bbox)))
    return xs, ys, out


# ── parsing ────────────────────────────────────────────────────────────────

INFO_LABELS = {
    "customer": ("khach hang", "ten khach hang", "customer", "customer name"),
    "address": ("dia chi cong trinh", "dia chi"),
    "phone": ("so dien thoai", "dien thoai", "sdt"),
    "date": ("ngay",),
    "customer_code": ("mkh", "ma khach hang"),
    "salesperson": ("nvkd", "nhan vien kinh doanh", "nhan vien phu trach"),
}


def _text_lines(page):
    out = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = "".join(s["text"] for s in line["spans"]).strip()
            if text:
                out.append((tuple(line["bbox"]), text, line["spans"][0]["size"]))
    return out


def _label_keys(label: str) -> set:
    """"Khách hàng/Customer name" → {"khach hang", "customer name"}."""
    return {norm(p) for p in re.split(r"[/(]", label.replace(")", "")) if p.strip()}


def _read_info(lines) -> dict:
    info = {}
    for i, (bbox, text, _) in enumerate(lines):
        if ":" not in text:
            continue
        label, _, value = text.partition(":")
        keys = _label_keys(label)
        for field_name, labels in INFO_LABELS.items():
            if keys & set(labels) and field_name not in info and value.strip():
                value = value.strip()
                # An address wrapped onto the next line (same column, no label).
                for nb, nt, _ in lines[i + 1:i + 3]:
                    if ":" in nt or abs(nb[0] - bbox[0]) > 3 or not 0 <= nb[1] - bbox[3] < 6:
                        break
                    value += " " + nt
                    bbox = nb
                info[field_name] = value
    return info


def _read_header(page, lines):
    """Company block, logo, title and 'Hạng mục' above the first table."""
    titled = [ln for ln in lines if norm(ln[1]).startswith(TITLE_WORDS)]
    title = max(titled, key=lambda ln: ln[2], default=None)
    if title is None:
        raise QuoteError("Không thấy tiêu đề báo giá / đơn hàng trong file.")
    title_top = title[0][1]
    company_lines = [t for (b, t, _) in sorted(lines, key=lambda ln: ln[0][1]) if b[3] <= title_top]
    category = next((t.partition(":")[2].strip() for (b, t, _) in lines
                     if norm(t).startswith("hang muc")), "")
    logos = [tuple(im["bbox"]) for im in page.get_image_info() if im["bbox"][3] <= title_top]
    logo = _png_uri(page, max(logos, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))) if logos else None
    return company_lines, category, logo


def _is_header_row(texts) -> bool:
    """A row whose cells are mostly column labels ("Khu vực", "Tên sản phẩm
    (Item)", "Số lượng (Quantity)"…) — at least 3 known labels."""
    hits = 0
    for t in texts:
        keys = _label_keys(t.replace("\n", " ")) if t else set()
        if any(k == w or k.startswith(w + " ") for k in keys for w in HEADER_WORDS):
            hits += 1
    return hits >= 3


def _parse_table(page, table, images, sections: list[Section], info_seen: set):
    xs, ys, cells = _grid(table)
    nrows, ncols = len(ys) - 1, len(xs) - 1
    by_row: dict[int, list] = {}
    for c in cells:
        by_row.setdefault(c[0], []).append(c)
    covered = set()
    for r0, c0, r1, c1, _ in cells:
        for r in range(r0 + 1, r1):
            covered.add(r)

    r = 0
    while r < nrows:
        row = sorted(by_row.get(r, []), key=lambda c: c[1])
        if not row:
            r += 1
            continue
        texts = [_cell_text(page, c[4]) for c in row]
        full_width = len(row) == 1 or (row[0][3] - row[0][1]) >= ncols - 3
        first = norm(texts[0])

        if full_width:
            joined = " ".join(t for t in texts if t)
            if not joined or ":" in texts[0] or norm(joined) in info_seen:
                r += 1
                continue
            if TOTAL_ROW.match(first):
                r += 1
                continue
            sections.append(Section(title=texts[0].replace("\n", " ")))
            r += 1
            continue

        # Header: the row that holds "Khu vực", plus the group row right above it.
        nxt = sorted(by_row.get(r + 1, []), key=lambda c: c[1])
        nxt_texts = [_cell_text(page, c[4]) for c in nxt]
        if _is_header_row(texts) or (nxt and _is_header_row(nxt_texts) and not _is_header_row(texts)):
            tall = any(c[2] - c[0] > 1 for c in row) and any(c[3] - c[1] > 1 for c in row)
            if _is_header_row(texts) and tall and nxt:
                # "Khu vực" spans two rows; the row below holds the group's sub-labels.
                group, sub, sub_texts, consumed = row, nxt, nxt_texts, 2
            elif _is_header_row(texts):
                group, sub, sub_texts, consumed = None, row, texts, 1
            else:
                group, sub, sub_texts, consumed = row, nxt, nxt_texts, 2
            labels = tuple(norm(t) for t in (texts + (nxt_texts if consumed == 2 else [])))
            if sections and sections[-1].labels == labels:
                r += consumed  # header repeated on the next page: same table goes on
                continue
            if not sections or sections[-1].header:
                sections.append(Section(title=sections[-1].title if sections else ""))
            _build_header(sections[-1], ncols, xs, group, texts if group else None, sub, sub_texts)
            sections[-1].labels = labels
            r += consumed
            continue

        sec = sections[-1] if sections and sections[-1].header else None
        if sec is None:
            r += 1
            continue
        if TOTAL_ROW.match(first) and all(not t or re.fullmatch(r"[\d.,\s%-]+", t) for t in texts[1:]):
            r += 1
            continue
        if not any(texts) and not any(_cell_image(page, c[4], images) for c in row):
            r += 1
            continue
        sec.rows.append(_build_row(sec, page, row, texts, images, ncols))
        r += 1


def _build_header(sec: Section, ncols, xs, group, group_texts, sub, sub_texts):
    """Column model: one label per grid column, grouped labels on top."""
    labels = [""] * ncols
    owner = list(range(ncols))  # grid col → first col of its header cell
    for (r0, c0, r1, c1, _), t in zip(sub, sub_texts):
        labels[c0] = t
        for c in range(c0, c1):
            owner[c] = c0
    groups = {}  # first grid col → (label, last col)
    if group:
        for (r0, c0, r1, c1, _), t in zip(group, group_texts):
            if r1 - r0 > 1:  # label cell spanning both header rows
                labels[c0] = t
                for c in range(c0, c1):
                    owner[c] = c0
            elif c1 - c0 > 1 and t:
                groups[c0] = (t, c1)
            elif t:  # "Khối" above "lượng (m2)": a wrapped label, not a group
                labels[c0] = (t + " " + labels[c0]).strip()
    keep = [c for c in range(ncols)
            if owner[c] == c and not DROP_COLUMN.search(norm(labels[c]))]
    sec.ncols = ncols
    sec.keep = keep
    sec.owner = owner
    total = xs[-1] - xs[0]
    span_end = {c: next((k for k in range(c + 1, ncols + 1) if k == ncols or owner[k] == k), ncols) for c in keep}
    widths = [(xs[span_end[c]] - xs[c]) / total for c in keep]

    # Insert "Check" before "Ghi chú" (or at the end).
    gi = next((i for i, c in enumerate(keep) if norm(labels[c]).startswith("ghi chu")), len(keep))
    sec.check_at = gi
    widths.insert(gi, 0.05)
    s = sum(widths)
    sec.widths = [round(100 * w / s, 2) for w in widths]

    header, sub_header, group_row = [], [], []
    i = 0
    cols = list(keep)
    while i < len(cols):
        c = cols[i]
        if i == gi:
            header.append(Cell("Check", rowspan=2 if groups else 1))
        g = next(((g0, lab, end) for g0, (lab, end) in groups.items() if g0 <= c < end), None)
        if g:
            members = [k for k in cols if g[0] <= k < g[2]]
            header.append(Cell(g[1], colspan=len(members)))
            sub_header.extend(labels[k] for k in members)
            i += len(members)
        else:
            header.append(Cell(labels[c], rowspan=2 if groups else 1))
            i += 1
    if gi == len(cols):
        header.append(Cell("Check", rowspan=2 if groups else 1))
    sec.header, sec.sub_header, sec.group_row = header, sub_header, group_row


def _build_row(sec: Section, page, row, texts, images, ncols):
    out = []
    keep = sec.keep
    cells_by_col = {c[1]: (c, t) for c, t in zip(row, texts)}
    for i, c in enumerate(keep):
        if i == sec.check_at:
            out.append(Cell(""))
        hit = cells_by_col.get(c)
        if hit is None:
            # covered by a vertical merge from an earlier row, or blank
            out.append(Cell(""))
            continue
        (r0, c0, r1, c1, bbox), text = hit
        span = len([k for k in keep if c0 <= k < c1]) or 1
        cell = Cell(text=vn_number(text), image=_cell_image(page, bbox, images), colspan=span)
        out.append(cell)
    if sec.check_at == len(keep):
        out.append(Cell(""))
    # drop placeholders swallowed by a colspan
    result, skip = [], 0
    for cell in out:
        if skip:
            skip -= 1
            if not cell.text and not cell.image:
                continue
        result.append(cell)
        skip = cell.colspan - 1
    return result


def parse_quote(pdf_bytes: bytes) -> dict:
    import fitz

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — any broken upload
        raise QuoteError("File không phải PDF hợp lệ.") from exc
    if doc.page_count == 0 or doc.page_count > MAX_PAGES:
        raise QuoteError(f"File phải có từ 1 đến {MAX_PAGES} trang.")

    first = doc[0]
    lines = _text_lines(first)
    company_lines, category, logo = _read_header(first, lines)
    info = _read_info(lines)
    info_seen = {norm(t) for (_, t, _) in lines if ":" in t}

    sections: list[Section] = []
    for page in doc:
        images = [tuple(im["bbox"]) for im in page.get_image_info()]
        for table in page.find_tables().tables:
            _parse_table(page, table, images, sections, info_seen)
    sections = [s for s in sections if s.header and s.rows]
    if not sections:
        raise QuoteError("Không đọc được bảng sản phẩm trong file (không thấy dòng tiêu đề cột).")

    head = norm(" ".join(company_lines))
    kind = "ordinaire" if "ordinaire" in head else "bonario"
    company = dict(COMPANIES[kind])
    if not company["legal_name"]:
        company["legal_name"] = company_lines[0] if company_lines else ""

    return {
        "kind": kind,
        "company_lines": company_lines,
        "logo": logo,
        "category": category,
        "company": company,
        "info": info,
        "sections": sections,
        "year": datetime.now(VN_TZ).year,
    }


# ── PDF + routes ───────────────────────────────────────────────────────────

MAX_UPLOAD = 15 * 1024 * 1024


def render_pdf(quote: dict) -> bytes:
    from flask import render_template
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    from delivery_report import FONTS_DIR, check_pdf_resource

    class LocalOnlyFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            check_pdf_resource(url)
            return super().fetch(url, headers)

    page = render_template("bbnt_from_quote.html", q=quote, orientation="portrait",
                           fonts_url=FONTS_DIR.resolve().as_uri())
    return HTML(string=page, base_url=FONTS_DIR.resolve().as_uri() + "/",
                url_fetcher=LocalOnlyFetcher(allowed_protocols={"data", "file"},
                                             fail_on_errors=False)).write_pdf()


def output_name(filename: str) -> str:
    stem = re.sub(r"\.pdf$", "", filename or "", flags=re.I)
    stem = re.sub(r"(?i)\b(bonario\s+)?(quotation|bao gia|báo giá)\b", "", stem)
    stem = re.sub(r"[^\w\- ]+", "", stem, flags=re.UNICODE)
    stem = re.sub(r"\s+", " ", stem).strip(" -_")
    return f"BBNT - {stem}.pdf" if stem else "BBNT.pdf"


def create_blueprint():
    from io import BytesIO
    from urllib.parse import quote as urlquote

    from flask import Blueprint, current_app, jsonify, render_template, request, send_file

    bp = Blueprint("bbnt", __name__)

    @bp.get("/bbnt")
    def bbnt_page():
        return render_template("bbnt.html", max_mb=MAX_UPLOAD // (1024 * 1024))

    @bp.post("/api/bbnt.pdf")
    def api_bbnt():
        if (request.content_length or 0) > MAX_UPLOAD + 64 * 1024:
            return jsonify(error=f"File quá lớn (tối đa {MAX_UPLOAD // (1024 * 1024)} MB).",
                           code="too_large"), 413
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify(error="Chưa chọn file báo giá.", code="no_file"), 400
        data = upload.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            return jsonify(error=f"File quá lớn (tối đa {MAX_UPLOAD // (1024 * 1024)} MB).",
                           code="too_large"), 413
        if not data.startswith(b"%PDF"):
            return jsonify(error="Chỉ nhận file PDF báo giá (xuất từ Excel ra PDF).",
                           code="not_pdf"), 400
        try:
            quote = parse_quote(data)
        except QuoteError as e:
            return jsonify(error=str(e), code="unreadable"), 422
        except Exception as e:  # noqa: BLE001 — malformed PDFs
            current_app.logger.warning("[BBNT] parse failed for %s: %s", upload.filename, e)
            return jsonify(error="Không đọc được file báo giá này.", code="unreadable"), 422
        pdf = render_pdf(quote)
        current_app.logger.info("[BBNT] %s → %d section(s), %d row(s)", upload.filename,
                                len(quote["sections"]), sum(len(s.rows) for s in quote["sections"]))
        resp = send_file(BytesIO(pdf), mimetype="application/pdf",
                         download_name=output_name(upload.filename))
        resp.headers["X-Filename"] = urlquote(output_name(upload.filename))
        return resp

    return bp
