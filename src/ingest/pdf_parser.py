"""Parse PDF đơn hàng: header (số đơn, ngày, đại lý, MST, địa chỉ) + bảng dòng hàng."""

from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber

from .models import OrderLine, SalesOrder

SO_NUMBER_RE = re.compile(r"BH\d{2}\.\d{4}")
DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
TAX_RE = re.compile(r"MST[:\s]+(\d{8,15})")
TNH_TAX = "0300397904"  # MST của Thống Nhất → bỏ qua khi tìm MST đại lý


def parse_pdf(source: Path | bytes, so_hint: str | None = None) -> SalesOrder:
    """Parse 1 PDF đơn hàng. `source` là `Path` hoặc `bytes` (lấy từ email attachment)."""

    fp: io.BytesIO | Path = io.BytesIO(source) if isinstance(source, bytes) else source

    with pdfplumber.open(fp) as pdf:
        text_pages = [page.extract_text() or "" for page in pdf.pages]
        tables: list[list[list[str]]] = []
        for page in pdf.pages:
            tables.extend(page.extract_tables() or [])

    text = "\n".join(text_pages)

    so_number = _find_so_number(text, so_hint)
    order_date = _find_order_date(text)
    customer_name = _find_customer_name(text)
    customer_tax = _find_customer_tax(text)
    customer_address = _find_customer_address(text)

    lines = _extract_lines(tables)
    if not lines:
        raise ValueError("PDF không có dòng hàng nào (table extraction trả về rỗng)")

    total_amount = sum((line.line_total for line in lines), Decimal(0))
    total_quantity = sum((int(line.quantity) for line in lines), 0)

    return SalesOrder(
        so_number=so_number,
        order_date=order_date,
        customer_name=customer_name,
        customer_tax_code=customer_tax,
        customer_address=customer_address,
        total_amount=total_amount,
        total_quantity=total_quantity,
        line_count=len(lines),
        lines=lines,
    )


# ─── header extraction ────────────────────────────────────────────────────────

def _find_so_number(text: str, hint: str | None) -> str:
    m = SO_NUMBER_RE.search(text)
    if m:
        return m.group(0)
    if hint:
        return hint
    raise ValueError("Không tìm thấy số đơn (BHxx.xxxx) trong PDF")


def _find_order_date(text: str) -> date:
    # Lấy ngày XX/XX/XXXX đầu tiên (sau "Ngày:")
    m = re.search(r"Ngày[:\s]+(\d{2})/(\d{2})/(\d{4})", text)
    if m:
        d, mo, y = m.groups()
        return date(int(y), int(mo), int(d))
    m = DATE_RE.search(text)
    if m:
        d, mo, y = m.groups()
        return date(int(y), int(mo), int(d))
    raise ValueError("Không tìm thấy ngày đặt (dd/mm/yyyy) trong PDF")


def _find_customer_name(text: str) -> str:
    m = re.search(r"Đại lý[:\s]+(.+?)(?:\s+MST[:\s]|\n)", text)
    if m:
        return m.group(1).strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("CÔNG TY", "CỬA HÀNG", "HỘ KINH DOANH", "DOANH NGHIỆP")):
            # Bỏ qua dòng tên Thống Nhất ở header
            if "THỐNG NHẤT" not in stripped.upper():
                return stripped
    return "UNKNOWN"


def _find_customer_tax(text: str) -> str | None:
    for tax in TAX_RE.findall(text):
        if tax != TNH_TAX:
            return tax
    return None


def _find_customer_address(text: str) -> str | None:
    m = re.search(r"Địa chỉ[:\s]+(.+?)(?:\nSTT|\n\d+\s|\nMã hàng|$)", text, re.DOTALL)
    if m:
        return " ".join(m.group(1).split())
    return None


# ─── table extraction ─────────────────────────────────────────────────────────

def _extract_lines(tables: list[list[list[str]]]) -> list[OrderLine]:
    """Tìm bảng product (header có 'STT' + 'Mã hàng') và parse từng row."""

    lines: list[OrderLine] = []
    for table in tables:
        if not table or len(table[0]) < 7:
            continue
        header_text = " ".join((c or "").lower() for c in table[0])
        if "stt" not in header_text or "mã hàng" not in header_text:
            continue
        for row in table[1:]:
            if len(row) < 7:
                continue
            stt_raw = (row[0] or "").strip()
            if not stt_raw.isdigit():
                continue  # row "Tổng:" không có STT
            try:
                lines.append(
                    OrderLine(
                        line_no=int(stt_raw),
                        product_code=(row[1] or "").strip(),
                        product_name=(row[2] or "").strip(),
                        unit=(row[3] or "Chiếc").strip() or "Chiếc",
                        quantity=_to_decimal(row[4]),
                        unit_price=_to_decimal(row[5]),
                        line_total=_to_decimal(row[6]),
                    )
                )
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(f"Row {stt_raw} không parse được: {row} ({exc})") from exc
    return lines


def _to_decimal(raw: str | None) -> Decimal:
    """Chuyển '1.898.148' (VN format) → Decimal. Hỗ trợ cả '1,5' (số lẻ qty)."""

    if raw is None:
        raise ValueError("Empty number cell")
    s = raw.strip()
    if not s:
        raise ValueError("Empty number cell")
    # VN: dấu chấm là thousand sep, dấu phẩy là decimal
    if "," in s:
        # '1.898.148,50' → '1898148.50'
        s = s.replace(".", "").replace(",", ".")
    else:
        # '1.898.148' → '1898148'
        s = s.replace(".", "")
    return Decimal(s)
