"""Ghi pipeline output thẳng vào Postgres (mode --write-db).

Behavior (đã được user duyệt):
1. Customer chưa có trong master (theo tax_code) → tạo `KH-NEW-NNNNN`
2. Product chưa có trong master (theo product_code) → tạo `SKU-NEW-NNNNN`
   với line_id=NULL, is_active=FALSE để cán bộ phòng SP map sau
3. Đơn đã có so_number → skip (idempotent, chạy lại không duplicate)
4. Sau khi insert xong, refresh `fact_sales` cho phạm vi T3/2026

Không update / không upsert — pipeline là append-only cho dữ liệu T3/2026.
Reset / migrate là việc của DBA bằng SQL riêng.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import psycopg
from loguru import logger

from .models import EmailLog, SalesOrder


@dataclass
class WriteStats:
    customers_inserted: int = 0
    products_inserted: int = 0
    orders_inserted: int = 0
    orders_skipped: int = 0       # so_number đã tồn tại
    lines_inserted: int = 0
    email_logs_inserted: int = 0
    fact_rows_inserted: int = 0


def write_to_db(
    db_url: str,
    orders: list[SalesOrder],
    email_logs: list[EmailLog],
    refresh_year: int,
    refresh_month: int,
) -> WriteStats:
    """Ghi orders + email_logs vào tnbike DB. Trả stats để summary."""
    stats = WriteStats()
    with psycopg.connect(db_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            # Trigger fn_update_order_totals UPDATE sales_order không qualify schema —
            # cần đặt tnbike vào search_path để trigger fire đúng bảng.
            cur.execute("SET search_path TO tnbike, public")
            existing_orders = _existing_so_numbers(cur)
            new_orders = [o for o in orders if o.so_number not in existing_orders]
            stats.orders_skipped = len(orders) - len(new_orders)
            if stats.orders_skipped:
                logger.info(
                    f"--write-db idempotent skip: {stats.orders_skipped} đơn đã có trong "
                    f"sales_order, sẽ không insert lại"
                )

            stats.customers_inserted = _upsert_customers(cur, new_orders)
            stats.products_inserted = _upsert_products(cur, new_orders)
            order_id_map = _insert_sales_orders(cur, new_orders)
            stats.orders_inserted = len(order_id_map)
            stats.lines_inserted = _insert_order_lines(cur, new_orders, order_id_map)
            stats.email_logs_inserted = _insert_email_logs(cur, email_logs)
            stats.fact_rows_inserted = _refresh_fact_sales(
                cur, refresh_year, refresh_month
            )
        conn.commit()
    return stats


# ─── helpers ─────────────────────────────────────────────────────────────────

def _existing_so_numbers(cur) -> set[str]:
    cur.execute("SELECT so_number FROM tnbike.sales_order")
    return {row[0] for row in cur.fetchall()}


def _next_seq(cur, prefix: str) -> int:
    """Lấy số tiếp theo cho `{prefix}-NNNNN` dựa trên customer_code/product_code lớn nhất hiện có."""
    table, col = ("customer", "customer_code") if prefix.startswith("KH") else ("product", "product_code")
    cur.execute(
        f"SELECT COALESCE(MAX(CAST(SUBSTRING({col} FROM '\\d+$') AS INT)), 0) "
        f"FROM tnbike.{table} WHERE {col} LIKE %s",
        (f"{prefix}%",),
    )
    return cur.fetchone()[0] + 1


def _upsert_customers(cur, orders: list[SalesOrder]) -> int:
    """Insert customer mới (KH-NEW-NNNNN) cho tax_code chưa có trong master."""
    cur.execute("SELECT tax_code FROM tnbike.customer WHERE tax_code IS NOT NULL")
    existing = {row[0] for row in cur.fetchall()}

    # Group theo tax_code, lấy tên đầu tiên gặp (orders đã sort theo so_number)
    new_by_tax: dict[str, tuple[str, str | None]] = {}
    for o in orders:
        tax = (o.customer_tax_code or "").strip()
        if not tax or tax in existing or tax in new_by_tax:
            continue
        new_by_tax[tax] = (o.customer_name, o.customer_address)

    if not new_by_tax:
        return 0

    seq = _next_seq(cur, "KH-NEW-")
    rows = []
    for tax, (name, addr) in sorted(new_by_tax.items()):
        rows.append((f"KH-NEW-{seq:05d}", name, tax, addr))
        seq += 1
    cur.executemany(
        "INSERT INTO tnbike.customer (customer_code, customer_name, tax_code, address) "
        "VALUES (%s, %s, %s, %s)",
        rows,
    )
    return len(rows)


def _upsert_products(cur, orders: list[SalesOrder]) -> int:
    """Insert product placeholder (SKU-NEW-NNNNN) cho product_code chưa có.

    Note: thực tế giữ product_code GỐC từ PDF (vd '1000400050040003'). Naming
    SKU-NEW-* chỉ dùng nếu product_code rỗng — extremely rare. Ở đây ta KHÔNG
    rename product_code mà chỉ INSERT row mới với code gốc + tên garbled từ PDF.
    Cán bộ phòng SP sau đó UPDATE tên đúng + map line_id.
    """
    cur.execute("SELECT product_code FROM tnbike.product")
    existing = {row[0] for row in cur.fetchall()}

    new_products: dict[str, str] = {}
    for o in orders:
        for line in o.lines:
            code = line.product_code.strip()
            if not code or code in existing or code in new_products:
                continue
            new_products[code] = line.product_name  # garbled, cán bộ update sau

    if not new_products:
        return 0

    rows = [(code, name, None, None, "Chiếc", False) for code, name in new_products.items()]
    cur.executemany(
        "INSERT INTO tnbike.product "
        "(product_code, product_name, line_id, color, unit, is_active) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (product_code) DO NOTHING",
        rows,
    )
    return len(new_products)


def _insert_sales_orders(cur, orders: list[SalesOrder]) -> dict[str, int]:
    """Insert sales_order, resolve customer_code qua tax_code. Trả map so_number → order_id."""
    if not orders:
        return {}

    cur.execute("SELECT tax_code, customer_code FROM tnbike.customer WHERE tax_code IS NOT NULL")
    tax_to_code = {row[0]: row[1] for row in cur.fetchall()}

    inserts = []
    for o in orders:
        tax = (o.customer_tax_code or "").strip()
        customer_code = tax_to_code.get(tax)
        if customer_code is None:
            raise ValueError(
                f"so_number={o.so_number}: tax_code {tax!r} không có trong customer "
                f"(lẽ ra _upsert_customers đã insert)"
            )
        inserts.append(
            (
                o.so_number,
                o.invoice_symbol,
                o.invoice_number,
                o.order_date,
                customer_code,
                o.total_amount,
                o.total_quantity,
                o.line_count,
            )
        )
    cur.executemany(
        "INSERT INTO tnbike.sales_order "
        "(so_number, invoice_symbol, invoice_number, order_date, customer_code, "
        " total_amount, total_quantity, line_count) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        inserts,
    )
    # Lấy lại order_id mới insert (so_number UNIQUE)
    so_numbers = tuple(o.so_number for o in orders)
    cur.execute(
        "SELECT so_number, order_id FROM tnbike.sales_order WHERE so_number = ANY(%s)",
        (list(so_numbers),),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def _insert_order_lines(
    cur, orders: list[SalesOrder], order_id_map: dict[str, int]
) -> int:
    """Insert order_line. Trigger trg_order_line_after_insert tự refresh sales_order totals."""
    rows = []
    for o in orders:
        oid = order_id_map.get(o.so_number)
        if oid is None:
            continue
        for line in o.lines:
            rows.append(
                (
                    oid,
                    o.so_number,
                    line.product_code,
                    line.quantity,
                    line.unit_price,
                    line.line_total,
                )
            )
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO tnbike.order_line "
        "(order_id, so_number, product_code, quantity, unit_price, line_total) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        rows,
    )
    return len(rows)


def _insert_email_logs(cur, logs: list[EmailLog]) -> int:
    if not logs:
        return 0
    rows = [
        (
            log.message_id,
            log.from_address or None,
            log.received_at,
            log.attachment_name,
            log.so_number,
            log.processing_status,
            log.error_message,
        )
        for log in logs
    ]
    # ON CONFLICT skip (idempotent — chạy lại cùng dataset không duplicate)
    cur.executemany(
        "INSERT INTO tnbike.email_log "
        "(message_id, from_address, received_at, attachment_name, so_number, processing_status, error_message) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (message_id) DO NOTHING",
        rows,
    )
    return len(rows)


def _refresh_fact_sales(cur, year: int, month: int) -> int:
    """Refresh fact_sales cho tháng cụ thể. Xóa rows cũ rồi insert lại để tránh duplicate."""
    cur.execute(
        "DELETE FROM tnbike.fact_sales WHERE fiscal_year = %s AND fiscal_month = %s",
        (year, month),
    )
    cur.execute(
        """
        INSERT INTO tnbike.fact_sales (
          order_date, fiscal_year, fiscal_quarter, fiscal_month, week_of_year,
          so_number, order_id, line_id,
          customer_code, customer_name, province_id, province_name, region,
          product_code, product_name, color, line_id_fk, line_name, group_code, group_name,
          quantity, unit_price, line_total
        )
        SELECT
          so.order_date, so.fiscal_year, so.fiscal_quarter, so.fiscal_month,
          EXTRACT(WEEK FROM so.order_date)::SMALLINT,
          ol.so_number, so.order_id, ol.line_id,
          c.customer_code, c.customer_name, c.province_id, pr.province_name, pr.region,
          p.product_code, p.product_name, p.color, p.line_id, pl.line_name, pg.group_code, pg.group_name,
          ol.quantity, ol.unit_price, ol.line_total
        FROM tnbike.order_line ol
        JOIN tnbike.sales_order so USING (order_id)
        JOIN tnbike.product p ON p.product_code = ol.product_code
        LEFT JOIN tnbike.product_line pl ON pl.line_id = p.line_id
        LEFT JOIN tnbike.product_group pg ON pg.group_code = pl.group_code
        LEFT JOIN tnbike.customer c ON c.customer_code = so.customer_code
        LEFT JOIN tnbike.province pr ON pr.province_id = c.province_id
        WHERE so.fiscal_year = %s AND so.fiscal_month = %s
        """,
        (year, month),
    )
    return cur.rowcount
