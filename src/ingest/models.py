"""Pydantic models — khớp với schema `tnbike` (sales_order / order_line / email_log)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class OrderLine(BaseModel):
    """Một dòng hàng trong PDF đơn hàng → tnbike.order_line."""

    line_no: int
    product_code: str
    product_name: str
    unit: str = "Chiếc"
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal

    @field_validator("product_code")
    @classmethod
    def _strip_code(cls, v: str) -> str:
        return v.strip()


class SalesOrder(BaseModel):
    """Đầu phiếu = 1 đại lý + N dòng hàng → tnbike.sales_order + tnbike.order_line."""

    so_number: str
    order_date: date
    customer_name: str
    customer_tax_code: str | None = None
    customer_address: str | None = None
    customer_code: str | None = None
    invoice_symbol: str | None = None
    invoice_number: str | None = None
    total_amount: Decimal
    total_quantity: int
    line_count: int
    lines: list[OrderLine] = Field(default_factory=list)


class EmailLog(BaseModel):
    """1 record / 1 email .eml đã xử lý → tnbike.email_log (DDL tạo riêng)."""

    message_id: str
    from_address: str
    received_at: datetime
    attachment_name: str | None = None
    so_number: str | None = None
    processing_status: str
    error_message: str | None = None
