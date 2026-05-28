"""Smoke tests trên 1 file PDF mẫu trong dataset gốc."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ingest.pdf_parser import parse_pdf
from ingest.validators import validate_order

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PDF = REPO_ROOT / "data" / "Emails & Files" / "tnbike_pdfs_mar2026" / "BH26_0935.pdf"


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="dataset PDF không có ở local")
def test_parse_sample_header():
    order = parse_pdf(SAMPLE_PDF)
    assert order.so_number == "BH26.0935"
    assert order.order_date.isoformat() == "2026-03-01"
    assert order.line_count >= 1
    assert order.total_amount > 0


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="dataset PDF không có ở local")
def test_parse_sample_lines_balance():
    order = parse_pdf(SAMPLE_PDF)
    for line in order.lines:
        assert line.quantity > 0
        assert line.unit_price > 0
        diff = abs(line.quantity * line.unit_price - line.line_total)
        assert diff <= Decimal("1"), f"Sai số quá lớn ở line {line.line_no}: {diff}"


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="dataset PDF không có ở local")
def test_validator_passes_on_clean_sample():
    order = parse_pdf(SAMPLE_PDF)
    assert validate_order(order) == []
