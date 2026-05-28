"""CLI: `ingest --source-dir "<Emails & Files>" --out output/`.

Output mặc định là 4 file CSV khớp schema `tnbike` (sẵn sàng `\\copy` vào Postgres):
  - sales_order.csv         (đơn pass hết rule)
  - order_line.csv          (lines của các đơn pass)
  - email_log.csv           (mọi đơn — gồm OK / REVIEW_REQUIRED / *_ERROR)
  - review_required.csv     (đơn cần cán bộ xử lý thủ công, có lý do)

Flag `--format excel` xuất 1 file `orders.xlsx` với 4 sheet tương ứng.
Flag `--db-url` (optional) để load master sets (customer.tax_code, product.product_code)
nhằm flag NEW_MST / UNKNOWN_PRODUCT.
"""

from __future__ import annotations

import csv
import os
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from loguru import logger
from tqdm import tqdm

from .customer_extractor import extract_customer, is_garbled_name, is_seller_name
from .email_parser import parse_eml
from .models import EmailLog, OrderLine, SalesOrder
from .pdf_parser import parse_pdf
from .validators import validate_order

# Phạm vi T3/2026 — pipeline build cho đề thi Vòng 2, fact_sales refresh trên cửa sổ này.
PIPELINE_YEAR = 2026
PIPELINE_MONTH = 3


class OutputFormat(str, Enum):
    csv = "csv"
    excel = "excel"


SALES_ORDER_COLS = [
    "so_number",
    "invoice_symbol",
    "invoice_number",
    "order_date",
    "customer_code",        # để trống — resolve sau khi load DB qua tax_code
    "customer_tax_code",    # helper col, không thuộc DDL
    "customer_name",        # helper col
    "customer_address",     # helper col
    "total_amount",
    "total_quantity",
    "line_count",
]

ORDER_LINE_COLS = [
    "so_number",
    "line_no",
    "product_code",
    "product_name",
    "unit",
    "quantity",
    "unit_price",
    "line_total",
]

EMAIL_LOG_COLS = [
    "message_id",
    "from_address",
    "received_at",
    "attachment_name",
    "so_number",
    "processing_status",
    "error_message",
]

REVIEW_COLS = [
    "so_number",
    "message_id",
    "received_at",
    "customer_tax_code",
    "customer_name",
    "review_flags",      # ; separated
    "suggested_action",
]

EMAIL_SUBDIR = "tnbike_emails_mar2026"
PDF_SUBDIR = "tnbike_pdfs_mar2026"

# Threshold cảnh báo cuối run — vượt qua thì log warning đề xuất cán bộ vào xử lý.
WARN_THRESHOLD = 0.10

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    source_dir: Path = typer.Option(
        ...,
        "--source-dir",
        "-s",
        exists=True,
        file_okay=False,
        help='Folder gốc "Emails & Files" (chứa tnbike_emails_mar2026/ và tnbike_pdfs_mar2026/)',
    ),
    out_dir: Path = typer.Option(
        Path("output"),
        "--out",
        "-o",
        help="Thư mục output (sẽ tạo nếu chưa có)",
    ),
    fmt: OutputFormat = typer.Option(
        OutputFormat.csv,
        "--format",
        "-f",
        case_sensitive=False,
        help="Định dạng output: csv (4 file rời) hoặc excel (1 file 4 sheet)",
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Chỉ chạy N file đầu — dùng để debug"
    ),
    db_url: str | None = typer.Option(
        None,
        "--db-url",
        envvar="DATABASE_URL",
        help="Postgres URL để load master sets cho NEW_MST / UNKNOWN_PRODUCT check. "
        "Bỏ trống thì skip check (kết quả không có 2 flag đó).",
    ),
    write_db: bool = typer.Option(
        False,
        "--write-db",
        help="Ghi thẳng vào Postgres (cần --db-url hoặc env DATABASE_URL). "
        "Tự tạo KH-NEW-* cho MST mới + SKU-NEW-* cho product mới (placeholder, "
        "is_active=FALSE), refresh fact_sales T3/2026. Idempotent: so_number đã có sẽ skip.",
    ),
):
    """Đọc .eml + PDF đính kèm trong --source-dir → xuất CSV/Excel khớp schema tnbike."""

    emails_dir = source_dir / EMAIL_SUBDIR
    pdf_dir: Path | None = source_dir / PDF_SUBDIR

    if not emails_dir.is_dir():
        raise typer.BadParameter(f"Không tìm thấy {emails_dir}")
    if not pdf_dir.is_dir():
        logger.warning(f"Không tìm thấy {pdf_dir} — sẽ chỉ dùng PDF từ email attachment")
        pdf_dir = None

    out_dir.mkdir(parents=True, exist_ok=True)
    eml_files = sorted(emails_dir.glob("*.eml"))
    if limit is not None:
        eml_files = eml_files[:limit]

    master_tax_codes, master_product_codes = _load_master_sets(db_url)

    logger.info(f"Source: {source_dir.resolve()}")
    logger.info(f"Tìm thấy {len(eml_files)} file .eml trong {EMAIL_SUBDIR}/")
    if master_tax_codes is not None:
        logger.info(
            f"Master loaded: {len(master_tax_codes)} customer.tax_code, "
            f"{len(master_product_codes or set())} product.product_code"
        )

    orders: list[SalesOrder] = []
    email_logs: list[EmailLog] = []
    review_rows: list[dict[str, Any]] = []

    for eml_path in tqdm(eml_files, desc="Parsing"):
        log, review = _process_one(
            eml_path, pdf_dir, orders, master_tax_codes, master_product_codes
        )
        email_logs.append(log)
        if review:
            review_rows.append(review)

    if fmt is OutputFormat.csv:
        _write_csv(out_dir, orders, email_logs, review_rows)
    else:
        _write_excel(out_dir / "orders.xlsx", orders, email_logs, review_rows)

    _print_summary(email_logs, review_rows)
    logger.info(f"Output → {out_dir.resolve()}")

    if write_db:
        if not db_url:
            raise typer.BadParameter("--write-db cần --db-url hoặc env DATABASE_URL")
        from .db_writer import write_to_db
        logger.info("--write-db: ghi vào Postgres…")
        stats = write_to_db(
            db_url, orders, email_logs,
            refresh_year=PIPELINE_YEAR, refresh_month=PIPELINE_MONTH,
        )
        logger.success(
            f"DB write done: {stats.orders_inserted} orders inserted, "
            f"{stats.orders_skipped} skipped (đã có so_number), "
            f"{stats.lines_inserted} lines, "
            f"{stats.customers_inserted} customer mới (KH-NEW-*), "
            f"{stats.products_inserted} product mới (SKU-NEW-*), "
            f"{stats.email_logs_inserted} email_log, "
            f"{stats.fact_rows_inserted} fact_sales rows (T{PIPELINE_MONTH}/{PIPELINE_YEAR})"
        )


# ─── master loader ────────────────────────────────────────────────────────────

def _load_master_sets(db_url: str | None) -> tuple[set[str] | None, set[str] | None]:
    """Đọc tax_code + product_code từ DB. Trả (None, None) nếu db_url rỗng / lỗi connect."""
    if not db_url:
        return None, None
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("psycopg chưa cài — bỏ qua master check. Cài: `uv sync --extra db`")
        return None, None
    try:
        with psycopg.connect(db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT tax_code FROM tnbike.customer WHERE tax_code IS NOT NULL")
            tax_codes = {row[0] for row in cur.fetchall() if row[0]}
            cur.execute("SELECT product_code FROM tnbike.product")
            product_codes = {row[0] for row in cur.fetchall() if row[0]}
    except Exception as exc:
        logger.warning(f"Không connect được DB ({exc}) — bỏ qua master check")
        return None, None
    return tax_codes, product_codes


# ─── per-file processing ──────────────────────────────────────────────────────

def _process_one(
    eml_path: Path,
    pdf_dir: Path | None,
    orders: list[SalesOrder],
    master_tax_codes: set[str] | None,
    master_product_codes: set[str] | None,
) -> tuple[EmailLog, dict[str, Any] | None]:
    try:
        msg = parse_eml(eml_path)
    except Exception as exc:
        logger.error(f"{eml_path.name}: lỗi parse email — {exc}")
        return (
            EmailLog(
                message_id=eml_path.stem,
                from_address="",
                received_at=datetime.now(),
                processing_status="PARSE_ERROR",
                error_message=str(exc),
            ),
            None,
        )

    pdf_bytes = msg.attachment_bytes
    attachment_name = msg.attachment_name

    if pdf_bytes is None and pdf_dir is not None:
        candidate = pdf_dir / f"{eml_path.stem}.pdf"
        if candidate.exists():
            pdf_bytes = candidate.read_bytes()
            attachment_name = candidate.name

    if pdf_bytes is None:
        return (
            EmailLog(
                message_id=msg.message_id,
                from_address=msg.from_address,
                received_at=msg.received_at,
                processing_status="NO_ATTACHMENT",
                error_message="Email không có file PDF đính kèm",
            ),
            None,
        )

    try:
        order = parse_pdf(pdf_bytes, so_hint=_so_hint_from_filename(eml_path.stem))
    except Exception as exc:
        logger.error(f"{eml_path.name}: lỗi parse PDF — {exc}")
        return (
            EmailLog(
                message_id=msg.message_id,
                from_address=msg.from_address,
                received_at=msg.received_at,
                attachment_name=attachment_name,
                processing_status="PARSE_ERROR",
                error_message=str(exc),
            ),
            None,
        )

    # Override customer info bằng dữ liệu từ email body (UTF-8 sạch).
    # PDF có font không có ToUnicode CMap đầy đủ cho tiếng Việt → tên/địa chỉ bị garbled.
    customer = extract_customer(msg.body_text)
    if customer.name:
        order.customer_name = customer.name
    if customer.tax_code:
        order.customer_tax_code = customer.tax_code
    if customer.address:
        order.customer_address = customer.address

    issues = validate_order(order)
    if issues:
        for issue in issues:
            logger.warning(f"{eml_path.name}: {issue}")
        return (
            EmailLog(
                message_id=msg.message_id,
                from_address=msg.from_address,
                received_at=msg.received_at,
                attachment_name=attachment_name,
                so_number=order.so_number,
                processing_status="VALIDATION_ERROR",
                error_message="; ".join(issues),
            ),
            None,
        )

    flags = _collect_review_flags(order, master_tax_codes, master_product_codes)

    # GARBLED_CUSTOMER_NAME hoặc SELLER_AS_CUSTOMER → reject, không xuất sales_order.
    # NEW_MST / UNKNOWN_PRODUCT là warning — đơn vẫn xuất ra, có thêm flag để cán bộ xử lý.
    hard_reject = {"GARBLED_CUSTOMER_NAME", "SELLER_AS_CUSTOMER"}
    if hard_reject & set(flags):
        review_row = _review_row(msg, order, flags)
        return (
            EmailLog(
                message_id=msg.message_id,
                from_address=msg.from_address,
                received_at=msg.received_at,
                attachment_name=attachment_name,
                so_number=order.so_number,
                processing_status="REVIEW_REQUIRED",
                error_message="; ".join(flags),
            ),
            review_row,
        )

    orders.append(order)
    review_row = _review_row(msg, order, flags) if flags else None
    return (
        EmailLog(
            message_id=msg.message_id,
            from_address=msg.from_address,
            received_at=msg.received_at,
            attachment_name=attachment_name,
            so_number=order.so_number,
            processing_status="REVIEW_REQUIRED" if flags else "OK",
            error_message="; ".join(flags) if flags else None,
        ),
        review_row,
    )


def _collect_review_flags(
    order: SalesOrder,
    master_tax_codes: set[str] | None,
    master_product_codes: set[str] | None,
) -> list[str]:
    flags: list[str] = []
    if is_garbled_name(order.customer_name):
        flags.append("GARBLED_CUSTOMER_NAME")
    if is_seller_name(order.customer_name):
        flags.append("SELLER_AS_CUSTOMER")
    if master_tax_codes is not None and order.customer_tax_code:
        if order.customer_tax_code not in master_tax_codes:
            flags.append("NEW_MST")
    if master_product_codes is not None:
        unknown = {l.product_code for l in order.lines if l.product_code not in master_product_codes}
        if unknown:
            flags.append(f"UNKNOWN_PRODUCT({len(unknown)})")
    return flags


def _review_row(msg, order: SalesOrder, flags: list[str]) -> dict[str, Any]:
    return {
        "so_number": order.so_number,
        "message_id": msg.message_id,
        "received_at": msg.received_at.isoformat() if msg.received_at else "",
        "customer_tax_code": order.customer_tax_code or "",
        "customer_name": order.customer_name,
        "review_flags": ";".join(flags),
        "suggested_action": _suggest(flags),
    }


def _suggest(flags: list[str]) -> str:
    """Tạo gợi ý hành động cho cán bộ. Trung thành data — không sửa nội dung."""
    suggestions = []
    for f in flags:
        if f == "GARBLED_CUSTOMER_NAME":
            suggestions.append("Liên hệ đại lý: email body thiếu thông tin KH, yêu cầu gửi lại đúng format")
        elif f == "SELLER_AS_CUSTOMER":
            suggestions.append("Tên trong body trùng seller (Thống Nhất) — xác minh ai là KH thật")
        elif f == "NEW_MST":
            suggestions.append("MST chưa có trong master — verify rồi tạo customer record mới (KH-NEW-*)")
        elif f.startswith("UNKNOWN_PRODUCT"):
            suggestions.append("Product_code không có trong master — verify với phòng SP, bổ sung SKU")
    return " | ".join(suggestions)


def _so_hint_from_filename(stem: str) -> str | None:
    if stem.startswith("BH") and "_" in stem:
        return stem.replace("_", ".", 1)
    return None


# ─── summary ──────────────────────────────────────────────────────────────────

def _print_summary(logs: list[EmailLog], reviews: list[dict[str, Any]]) -> None:
    total = len(logs)
    counts: dict[str, int] = {}
    for log in logs:
        counts[log.processing_status] = counts.get(log.processing_status, 0) + 1

    flag_counts: dict[str, int] = {}
    for r in reviews:
        for f in r["review_flags"].split(";"):
            key = f.split("(", 1)[0]  # gom UNKNOWN_PRODUCT(3) vào UNKNOWN_PRODUCT
            flag_counts[key] = flag_counts.get(key, 0) + 1

    ok = counts.get("OK", 0)
    review = counts.get("REVIEW_REQUIRED", 0)
    err = total - ok - review

    logger.success(f"Hoàn tất: {total} đơn · {ok} OK · {review} REVIEW_REQUIRED · {err} lỗi")
    if counts:
        for status, n in sorted(counts.items()):
            logger.info(f"  {status:18s} {n:5d}  ({n/total:.1%})")
    if flag_counts:
        logger.info("Review flags breakdown:")
        for flag, n in sorted(flag_counts.items(), key=lambda x: -x[1]):
            ratio = n / total
            level = "⚠️ " if ratio > WARN_THRESHOLD else "  "
            logger.info(f"  {level}{flag:25s} {n:5d}  ({ratio:.1%})")
        over = [f for f, n in flag_counts.items() if n / total > WARN_THRESHOLD]
        if over:
            logger.warning(
                f"Vượt threshold {WARN_THRESHOLD:.0%}: {', '.join(over)} → đề xuất cán bộ vào xử lý batch "
                f"(liên hệ đại lý / phòng SP)"
            )


# ─── CSV output ───────────────────────────────────────────────────────────────

def _write_csv(
    out_dir: Path,
    orders: list[SalesOrder],
    logs: list[EmailLog],
    reviews: list[dict[str, Any]],
) -> None:
    _write_csv_rows(
        out_dir / "sales_order.csv",
        SALES_ORDER_COLS,
        (_order_row(o) for o in orders),
    )
    _write_csv_rows(
        out_dir / "order_line.csv",
        ORDER_LINE_COLS,
        (_line_row(o.so_number, line) for o in orders for line in o.lines),
    )
    _write_csv_rows(
        out_dir / "email_log.csv",
        EMAIL_LOG_COLS,
        (_log_row(log) for log in logs),
    )
    _write_csv_rows(
        out_dir / "review_required.csv",
        REVIEW_COLS,
        iter(reviews),
    )


def _write_csv_rows(path: Path, cols: list[str], rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=cols, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ─── Excel output ─────────────────────────────────────────────────────────────

def _write_excel(
    path: Path,
    orders: list[SalesOrder],
    logs: list[EmailLog],
    reviews: list[dict[str, Any]],
) -> None:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise typer.BadParameter(
            "Excel output cần `openpyxl`. Cài: `uv sync --extra excel` hoặc `pip install openpyxl`."
        ) from exc

    wb = Workbook()
    wb.remove(wb.active)  # bỏ sheet mặc định

    _write_xlsx_sheet(wb, "sales_order", SALES_ORDER_COLS, [_order_row(o) for o in orders])
    _write_xlsx_sheet(
        wb,
        "order_line",
        ORDER_LINE_COLS,
        [_line_row(o.so_number, line) for o in orders for line in o.lines],
    )
    _write_xlsx_sheet(wb, "email_log", EMAIL_LOG_COLS, [_log_row(log) for log in logs])
    _write_xlsx_sheet(wb, "review_required", REVIEW_COLS, reviews)

    wb.save(path)


def _write_xlsx_sheet(wb, name: str, cols: list[str], rows: list[dict]) -> None:
    ws = wb.create_sheet(name)
    ws.append(cols)
    for row in rows:
        ws.append([row.get(c) for c in cols])


# ─── row serialization ────────────────────────────────────────────────────────

def _order_row(o: SalesOrder) -> dict[str, Any]:
    return {
        "so_number": o.so_number,
        "invoice_symbol": o.invoice_symbol or "",
        "invoice_number": o.invoice_number or "",
        "order_date": o.order_date.isoformat(),
        "customer_code": o.customer_code or "",
        "customer_tax_code": o.customer_tax_code or "",
        "customer_name": o.customer_name,
        "customer_address": o.customer_address or "",
        "total_amount": _fmt_decimal(o.total_amount),
        "total_quantity": o.total_quantity,
        "line_count": o.line_count,
    }


def _line_row(so_number: str, line: OrderLine) -> dict[str, Any]:
    return {
        "so_number": so_number,
        "line_no": line.line_no,
        "product_code": line.product_code,
        "product_name": line.product_name,
        "unit": line.unit,
        "quantity": _fmt_decimal(line.quantity),
        "unit_price": _fmt_decimal(line.unit_price),
        "line_total": _fmt_decimal(line.line_total),
    }


def _log_row(log: EmailLog) -> dict[str, Any]:
    return {
        "message_id": log.message_id,
        "from_address": log.from_address,
        "received_at": log.received_at.isoformat() if log.received_at else "",
        "attachment_name": log.attachment_name or "",
        "so_number": log.so_number or "",
        "processing_status": log.processing_status,
        "error_message": log.error_message or "",
    }


def _fmt_decimal(d: Decimal) -> str:
    """Decimal → string không đổi format. CSV/Postgres parse được trực tiếp."""
    return format(d, "f")  # tránh notation khoa học


def _to_iso(o: Any) -> str:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


if __name__ == "__main__":
    app()
