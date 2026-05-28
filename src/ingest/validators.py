"""Cross-check sau khi parse: số học, dấu, sum totals.

Tolerance: hỗn hợp absolute + relative — buffer làm tròn nhiều bước trong PDF nguồn,
auto-scale theo giá trị đơn để không kẹt với đơn lớn (>100M VND).
"""

from __future__ import annotations

from decimal import Decimal

from .models import SalesOrder

ABS_TOLERANCE = Decimal("10")        # ±10 đồng cho đơn nhỏ
REL_TOLERANCE = Decimal("0.00001")   # ±0.001% cho đơn lớn — đủ để bắt lỗi data thật


def _tolerance(value: Decimal) -> Decimal:
    return max(ABS_TOLERANCE, abs(value) * REL_TOLERANCE)


def validate_order(order: SalesOrder) -> list[str]:
    """Trả về list issue (string). Empty list = OK."""

    issues: list[str] = []

    if not order.lines:
        issues.append("Đơn không có dòng hàng nào")
        return issues

    for line in order.lines:
        expected = (line.quantity * line.unit_price).quantize(Decimal("1"))
        actual = line.line_total.quantize(Decimal("1"))
        if abs(expected - actual) > _tolerance(line.line_total):
            issues.append(
                f"Line {line.line_no} ({line.product_code}): "
                f"qty × price = {expected} ≠ line_total {actual}"
            )
        if line.quantity <= 0:
            issues.append(f"Line {line.line_no}: quantity ≤ 0")
        if line.unit_price < 0:
            issues.append(f"Line {line.line_no}: unit_price < 0")

    sum_lines = sum((line.line_total for line in order.lines), Decimal(0))
    if abs(sum_lines - order.total_amount) > _tolerance(order.total_amount):
        issues.append(
            f"Sum line_total ({sum_lines}) ≠ total_amount header ({order.total_amount})"
        )

    return issues
