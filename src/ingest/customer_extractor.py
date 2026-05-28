"""Trích xuất thông tin đại lý từ phần body email (UTF-8 chuẩn).

PDF có Vietnamese diacritics bị garbled do font ReportLab thiếu ToUnicode CMap đầy đủ
→ phải lấy customer_name / address / tax từ email body, không phải từ PDF.

Body có 3 format thường gặp (xem tnbike_emails_mar2026/*.eml):
  A. "Khách hàng : <name>" / "MST : ..." / "Địa chỉ : ..."
  B. "Thông tin đại lý:\\n  Tên : <name>" / "MST : ..."
  C. "Đại lý : <name>" / "MST : ..."
"""

from __future__ import annotations

import re
from typing import NamedTuple


class CustomerInfo(NamedTuple):
    name: str | None
    tax_code: str | None
    address: str | None
    phone: str | None


# Mỗi field thử nhiều label theo thứ tự ưu tiên. Bắt đúng dòng (.+ không match \n)
# để tránh nuốt nhầm dòng kế tiếp như regex cũ.
_NAME_PATTERNS = [
    re.compile(r"^\s*Kh[áa]ch\s*h[àa]ng\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Đ[ạa]i\s*l[ýy]\s*[:：]\s*(?!\s*$)(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*T[êe]n(?:\s*đại\s*lý)?\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Đ[ơo]n\s*v[ịi]\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE),
]
_TAX_PATTERN = re.compile(r"\bMST\s*[:：]\s*(\d{8,15})", re.IGNORECASE)
_ADDR_PATTERN = re.compile(r"^\s*Đ[ịi]a\s*ch[ỉi]\s*[:：]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_PHONE_PATTERN = re.compile(
    r"^\s*(?:Li[êe]n\s*h[ệe]|Đi[ệe]n\s*tho[ạa]i|S[ĐD]T|Tel)\s*[:：]\s*([\d\s.\-+()]+)$",
    re.IGNORECASE | re.MULTILINE,
)

# Pattern phát hiện garbled name (PDF fallback bị hỏng font Vietnamese).
# Bắt mẫu "Thnng Nhnt", "Cn PHnN", "nnp", "Hnng" và các biến thể uppercase/lowercase.
_GARBLED_NAME_RE = re.compile(
    r"thnng|nhnt|cn\s*ph[ae]n|\bnnp\b|hnng\b|■",
    re.IGNORECASE,
)
# Tên seller — xuất hiện ở header PDF, không phải tên đại lý.
_SELLER_NAME_RE = re.compile(r"thống\s*nhất|thong\s*nhat", re.IGNORECASE)


def extract_customer(body: str) -> CustomerInfo:
    """Parse email body → CustomerInfo. Trường nào không tìm thấy = None."""

    name = _first_match(_NAME_PATTERNS, body)
    tax_match = _TAX_PATTERN.search(body)
    addr_match = _ADDR_PATTERN.search(body)
    phone_match = _PHONE_PATTERN.search(body)

    return CustomerInfo(
        name=_clean_name(name) if name else None,
        tax_code=tax_match.group(1).strip() if tax_match else None,
        address=_clean(addr_match.group(1)) if addr_match else None,
        phone=_clean(phone_match.group(1)) if phone_match else None,
    )


def is_garbled_name(name: str | None) -> bool:
    """True nếu tên có dấu hiệu garbled từ font PDF (cần REVIEW_REQUIRED)."""
    return bool(name) and bool(_GARBLED_NAME_RE.search(name))


def is_seller_name(name: str | None) -> bool:
    """True nếu tên match seller Thống Nhất (sau khi normalize, không tính bản garbled)."""
    return bool(name) and bool(_SELLER_NAME_RE.search(name))


def _first_match(patterns: list[re.Pattern[str]], body: str) -> str | None:
    for p in patterns:
        m = p.search(body)
        if m and m.group(1).strip():
            return m.group(1)
    return None


def _clean(s: str) -> str:
    """Cắt sau ký tự xuống dòng đầu tiên, strip whitespace, normalize spaces."""
    first_line = s.splitlines()[0] if s else ""
    return re.sub(r"\s+", " ", first_line).strip()


def _clean_name(s: str) -> str:
    """Như _clean nhưng bỏ thêm prefix 'Tên :' / 'Tên đại lý :' nếu còn sót."""
    cleaned = _clean(s)
    cleaned = re.sub(r"^T[êe]n(?:\s*đại\s*lý)?\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()
