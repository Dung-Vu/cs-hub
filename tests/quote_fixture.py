"""Builds a fake Sales quotation PDF (no real customer data) for the BBNT tests."""

from __future__ import annotations

import base64
import struct
import zlib

QUOTE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size: A4 landscape; margin: 10mm; }}
body {{ font-family: sans-serif; font-size: 9px; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #000; padding: 3px; text-align: center; }}
h1 {{ text-align: center; font-size: 20px; }}
</style></head><body>
<img src="{logo}" style="width:80px;height:40px">
<p><b>CÔNG TY MẪU NỘI THẤT</b></p>
<p>Showroom: 1 Đường Thử, Quận Mẫu</p>
<h1>BẢNG BÁO GIÁ</h1>
<p>Hạng mục: Rèm cửa</p>
<p>Khách hàng: Anh Khách Mẫu</p>
<p>Địa chỉ công trình: 1 Đường Giả Định</p>
<p>Số điện thoại: 0900 000 000</p>
<table>
<tr><th colspan="9">PHÒNG NGỦ</th></tr>
<tr><th rowspan="2">Khu Vực</th><th rowspan="2">Vị trí</th><th rowspan="2">Mã Sản Phẩm</th>
    <th colspan="2">Thông số kích thước</th><th rowspan="2">Khối lượng (m2)</th>
    <th rowspan="2">Đơn giá</th><th rowspan="2">Thành Tiền</th><th rowspan="2">Ghi chú</th></tr>
<tr><th>Ngang (m)</th><th>Cao (m)</th></tr>
<tr><td>CỬA SỔ</td><td>1</td><td>MAU-01<br><img src="{swatch}" style="width:40px;height:40px"></td>
    <td>3.00</td><td>1.60</td><td>4.80</td><td>429,133</td><td>2,059,840</td><td></td></tr>
<tr><td>CỬA ĐI</td><td>2</td><td>MAU-02</td>
    <td>1.20</td><td>2.50</td><td>3.00</td><td>429,133</td><td>1,287,399</td><td>Treo cao</td></tr>
<tr><td colspan="7">Tổng cộng</td><td>3,347,239</td><td></td></tr>
</table>
</body></html>"""


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> str:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


ORDER_HEAD = """<tr><th>Số thứ tự (No.)</th><th>Tên sản phẩm (Item)</th><th>Hình ảnh (Image)</th>
<th>Chi tiết (Description)</th><th>Số lượng (Quantity)</th><th>Đơn giá (Unit Price)</th>
<th>% Giảm giá (Discount %)</th><th>Thành tiền đã bao gồm VAT (Total)</th><th>Ghi chú (Note)</th></tr>"""

ORDER_ROW = """<tr><td>{n}</td><td>MAU-SOFA-{n}</td><td><img src="{img}" style="width:40px;height:40px"></td>
<td>- Vật liệu: gỗ mẫu</td><td>{n} Cái</td><td>12,500,000</td><td>10</td><td>11,250,000</td><td></td></tr>"""

ORDER_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size: A4 landscape; margin: 10mm; }}
body {{ font-family: sans-serif; font-size: 9px; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #000; padding: 3px; text-align: center; }}
h1 {{ text-align: center; font-size: 24px; margin: 4px; }} h2 {{ text-align: center; font-size: 16px; margin: 0; }}
</style></head><body>
<p>Store Mẫu | 1 Đường Thử, Quận Mẫu</p>
<p>Website: www.ordinaire.example</p>
<h1>ĐƠN HÀNG</h1><h2>Sales Order</h2>
<p>Khách hàng/Customer name: Công Ty Khách Mẫu</p>
<p style="margin:0">Địa chỉ/Address: 1 Đường Giả Định</p>
<p style="margin:0">Phường Mẫu Thành phố Thử</p>
<p>Điện thoại/Phone: 0900 000 001</p>
<table>{head}{first}</table>
<div style="page-break-before: always"></div>
<table>{head}{rest}</table>
<p>Tổng cộng (Total): 33,750,000</p>
</body></html>"""


def synthetic_order_pdf() -> bytes:
    """Ordinaire-style order printed from Odoo: bilingual labels, no "Khu vực",
    item table split across two pages with the header repeated."""
    from weasyprint import HTML

    img = _png(8, 8, (90, 160, 90))
    rows = [ORDER_ROW.format(n=n, img=img) for n in (1, 2, 3)]
    html = ORDER_HTML.format(head=ORDER_HEAD, first=rows[0], rest="".join(rows[1:]))
    return HTML(string=html).write_pdf()


def synthetic_quote_pdf() -> bytes:
    from weasyprint import HTML

    html = QUOTE_HTML.format(logo=_png(16, 8, (200, 120, 60)), swatch=_png(8, 8, (90, 90, 160)))
    return HTML(string=html).write_pdf()
