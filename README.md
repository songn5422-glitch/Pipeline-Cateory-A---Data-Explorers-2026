# Pipeline A — TNH Bike Order Ingestion

Pipeline xử lý đơn hàng tự động cho **DATA EXPLORERS 2026 — Vòng 2 / Hạng mục A / Phương án A** (25 điểm). Đầu vào: 1.132 file `.eml` + 1.132 PDF đính kèm tháng 3/2026. Đầu ra: 4 file CSV (hoặc Excel 4 sheet) khớp schema `tnbike` (Postgres) + ghi thẳng vào database qua flag `--write-db`.

---

## Mục lục

1. [Kết quả thực tế](#1-kết-quả-thực-tế)
2. [Kiến trúc pipeline](#2-kiến-trúc-pipeline)
3. [Cài đặt](#3-cài-đặt)
4. [Setup Postgres local](#4-setup-postgres-local-optional-cho-master-check--write-db)
5. [Chuẩn bị dữ liệu](#5-chuẩn-bị-dữ-liệu)
6. [Chạy pipeline](#6-chạy-pipeline)
7. [Output schema](#7-output-schema)
8. [Review flag taxonomy](#8-review-flag-taxonomy)
9. [Load vào Postgres](#9-load-vào-postgres)
10. [Test](#10-test)
11. [Cấu trúc thư mục](#11-cấu-trúc-thư-mục)
12. [Lưu ý kỹ thuật](#12-lưu-ý-kỹ-thuật)
13. [Tài liệu liên quan](#13-tài-liệu-liên-quan)

---

## 1. Kết quả thực tế

Trên toàn bộ 1.132 đơn T3/2026, run trên WSL2 Ubuntu 24.04 + Python 3.12.3 + Postgres 16.13:

| Chỉ số | Giá trị |
|---|---:|
| Thời gian parse 1.132 file | ~50 giây |
| Đơn parse OK | 850 (75.1%) |
| Đơn REVIEW_REQUIRED (đã ghi DB + flag cho cán bộ) | 282 (24.9%) |
| Đơn PARSE_ERROR / VALIDATION_ERROR / NO_ATTACHMENT | 0 |
| **Sau `--write-db` — DB state** | |
| `sales_order` T3/2026 | 1.132 đơn (100%) |
| `order_line` T3/2026 | 8.723 lines (100%) |
| `fact_sales` T3/2026 | 8.723 rows (100%) |
| `customer` KH-NEW-* tự tạo | 96 |
| `product` SKU placeholder (is_active=FALSE) tự tạo | 18 |
| Tổng doanh thu T3/2026 | 40,8 tỷ VND |

Breakdown của 282 REVIEW_REQUIRED:
- 234 đơn `NEW_MST` (20.7%) — MST chưa có trong customer master 702 KH
- 78 đơn có ít nhất 1 line `UNKNOWN_PRODUCT` (6.9%) — SKU chưa có trong product master 247 SKU

→ Cả 2 đều là **business event** (đại lý mới đặt hàng / SKU mới release), không phải lỗi data. Pipeline tự tạo placeholder + flag cho cán bộ verify thay vì sửa data gốc. Chi tiết xử lý xem [`docs/data-quality.md`](docs/data-quality.md).

---

## 2. Kiến trúc pipeline

```
                                  ┌──────────────────────────────────┐
.eml ─► email_parser ──► header + body (UTF-8) + PDF bytes           │
                                  │                                   │
                                  ▼                                   │
                          pdf_parser ──► so_number, ngày, table lines │
                                  │                                   │
                                  ▼                                   │
                  customer_extractor ──► tên/MST/địa chỉ (UTF-8 sạch) │
                                  │                                   │
                                  ▼                                   │
                          validators ──► qty × price = line_total     │
                                  │                                   │
                                  ▼                                   │
                       review_flags check  ◄─── master sets (DB)      │
                                  │             tax_code + product    │
                                  ▼                                   │
                            ┌─────┴─────┐                             │
                            ▼           ▼                             │
                       CSV / Excel    db_writer (─-write-db)          │
                       (4 file/sheet)   │                             │
                                        ├─► INSERT customer KH-NEW-*  │
                                        ├─► INSERT product SKU-NEW-*  │
                                        ├─► INSERT sales_order        │
                                        ├─► INSERT order_line         │
                                        ├─► INSERT email_log          │
                                        └─► REFRESH fact_sales T3/26 ─┘
```

Mỗi module có 1 trách nhiệm rõ ràng:
- `email_parser.py` — đọc `.eml`, decode header + body + tách PDF attachment (MIME)
- `pdf_parser.py` — đọc PDF bằng `pdfplumber`, extract header + bảng line
- `customer_extractor.py` — trích tên/MST/địa chỉ đại lý từ body UTF-8 sạch
- `validators.py` — cross-check số học (qty × price = line_total, sum lines = total_amount)
- `db_writer.py` — ghi DB tự động cho mode `--write-db`
- `cli.py` — orchestrator + CSV/Excel writer + summary

### Tại sao customer info lấy từ email body, không phải PDF?

PDF do ReportLab generate dùng font **Helvetica với WinAnsi encoding** — không có ToUnicode CMap đầy đủ cho Vietnamese diacritics. Cả `pdfplumber` lẫn `pypdfium2` đều render `Đặt`, `Phòng`, `Trống` thành `■` hoặc `n`. Email body là `text/plain; charset=utf-8` quoted-printable → decode chuẩn 100%.

Hệ quả:
- `customer_name` / `customer_address` / `customer_tax_code` lấy từ **body**
- `product_name` từ PDF vẫn còn garbled (vd `Xe nnp Thnng Nhnt Tom & Jerry 14 Hnng`) → khi load DB, query `JOIN tnbike.product` để lấy tên đẹp; nếu SKU chưa có trong master, pipeline tự tạo placeholder với tên garbled và flag `is_active=FALSE` cho cán bộ phòng SP update sau.

---

## 3. Cài đặt

Yêu cầu: **Python 3.12+**.

### uv (khuyến nghị — nhanh, lock file deterministic)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # cài uv 1 lần

uv sync                              # core deps
uv sync --extra excel                # xuất .xlsx
uv sync --extra db                   # bật --write-db mode (cần psycopg)
uv sync --extra dev                  # pytest / ruff
uv sync --extra excel --extra db --extra dev   # full
```

### pip

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
# Tùy chọn: pip install -e ".[excel,db,dev]"
```

---

## 4. Setup Postgres local (optional — cho master check + `--write-db`)

Pipeline chạy được mà **không cần** DB (mode CSV thuần). Bật DB để:
- Master check: pipeline tự flag `NEW_MST` / `UNKNOWN_PRODUCT` bằng cách so với `tnbike.customer` + `tnbike.product`.
- `--write-db` mode: ghi thẳng vào Postgres + refresh `fact_sales` tự động.

```bash
# Ubuntu 24.04 / Debian
sudo apt-get install -y postgresql postgresql-contrib
sudo systemctl enable --now postgresql

# Tạo role + DB (đổi <PASSWORD> bằng password thật)
sudo -u postgres psql -c "CREATE ROLE duongdd LOGIN SUPERUSER PASSWORD '<PASSWORD>';"
sudo -u postgres createdb -O duongdd -E UTF8 tnbike_db

# Apply DDL + dataset baseline BTC (T1/2025–T2/2026)
psql -U duongdd -h localhost -d tnbike_db -f Database/01_create_tables.sql
psql -U duongdd -h localhost -d tnbike_db -f Database/02_import_data.sql

# Tạo bảng email_log (chưa có trong DDL gốc của BTC)
psql -U duongdd -h localhost -d tnbike_db <<'EOF'
CREATE TABLE tnbike.email_log (
  log_id            BIGSERIAL    PRIMARY KEY,
  message_id        TEXT         NOT NULL UNIQUE,
  from_address      TEXT,
  received_at       TIMESTAMPTZ,
  attachment_name   TEXT,
  so_number         VARCHAR(20),
  processing_status TEXT         NOT NULL,
  error_message     TEXT,
  processed_at      TIMESTAMPTZ  DEFAULT NOW()
);
EOF

# Setup .env (đừng commit — đã gitignored)
cp .env.example .env
# Mở .env và thay <PASSWORD> bằng password vừa tạo
```

---

## 5. Chuẩn bị dữ liệu

Đặt dataset BTC cấp vào folder `Emails & Files/` ở root repo:

```
data-explorers-2026/
└── Emails & Files/
    ├── tnbike_emails_mar2026/   # 1.132 file .eml
    └── tnbike_pdfs_mar2026/     # 1.132 file .pdf
```

`Emails & Files/`, `Database/`, `*.pdf`, `*.xlsx`, `*.docx` đều có trong `.gitignore` — không commit dataset BTC.

---

## 6. Chạy pipeline

`--source-dir` trỏ thẳng vào folder gốc `Emails & Files`. Pipeline tự tìm:
- `<source-dir>/tnbike_emails_mar2026/*.eml` — primary input
- `<source-dir>/tnbike_pdfs_mar2026/<stem>.pdf` — fallback nếu `.eml` thiếu attachment

```bash
# Source .env để load DATABASE_URL (cho master check + --write-db)
set -a; source .env; set +a
```

### Mode 1: CSV thuần (không cần DB)

```bash
uv run ingest --source-dir "Emails & Files" --out output/
```

Xuất 4 file CSV vào `output/`. Không kết nối DB. Không có flag `NEW_MST` / `UNKNOWN_PRODUCT` (vì không có master để so).

### Mode 2: CSV + master check (cần DB)

```bash
# Tự nhận DATABASE_URL từ env, master check bật, không ghi DB
uv run ingest --source-dir "Emails & Files" --out output/
```

Có DATABASE_URL → pipeline tự đọc master sets (`customer.tax_code`, `product.product_code`) để flag chính xác đơn nào có MST mới / SKU mới.

### Mode 3: `--write-db` — ghi thẳng vào Postgres (recommended cho production)

```bash
uv run ingest --source-dir "Emails & Files" --out output/ --write-db
```

Một lệnh duy nhất sẽ:
1. Parse 1.132 đơn (~50 giây)
2. Xuất 4 file CSV vào `output/` (audit trail)
3. Connect Postgres qua `DATABASE_URL`
4. Tự tạo `KH-NEW-NNNNN` cho 96 MST mới
5. Tự tạo SKU placeholder cho 18 product_code mới (`is_active=FALSE`)
6. INSERT 1.132 sales_order + 8.723 order_line (trigger tự refresh totals)
7. INSERT 1.132 email_log
8. DELETE + INSERT 8.723 rows fact_sales T3/2026

**Idempotent**: chạy lại trên dataset đã ghi → đơn có `so_number` tồn tại sẽ skip, không duplicate.

### Mode 4: Excel output (1 file 4 sheet)

```bash
uv run ingest --source-dir "Emails & Files" --out output/ --format excel
```

Xuất `output/orders.xlsx` gồm 4 sheet: `sales_order`, `order_line`, `email_log`, `review_required`. Mode này độc lập với `--write-db` — có thể combine.

### Mode 5: Debug nhanh trên 10 đơn

```bash
uv run ingest --source-dir "Emails & Files" --out output/ --limit 10
```

---

## 7. Output schema

### CSV (default) — 4 file rời, ready cho `\copy` vào Postgres

| File | Schema | Note |
|---|---|---|
| `sales_order.csv` | `tnbike.sales_order` + 3 helper col (`customer_tax_code`, `customer_name`, `customer_address`) | `customer_code` để trống — resolve ở DB qua tax_code lookup. KHÔNG chứa đơn bị hard reject (`GARBLED_CUSTOMER_NAME` / `SELLER_AS_CUSTOMER`). |
| `order_line.csv` | `tnbike.order_line` (`so_number`, `line_no`, `product_code`, `product_name`, `unit`, `quantity`, `unit_price`, `line_total`) | `order_id` resolve ở DB qua `so_number` JOIN |
| `email_log.csv` | `tnbike.email_log` | `processing_status` ∈ `OK / REVIEW_REQUIRED / NO_ATTACHMENT / PARSE_ERROR / VALIDATION_ERROR` |
| `review_required.csv` | `so_number, message_id, received_at, customer_tax_code, customer_name, review_flags, suggested_action` | Danh sách đơn cán bộ cần xử lý thủ công, gồm flag + gợi ý hành động. |

### Excel — 1 file `orders.xlsx` với 4 sheet trùng tên CSV

---

## 8. Review flag taxonomy

Nguyên tắc **trung thành data gốc**: pipeline không sửa nội dung, chỉ flag + suggest action.

| Flag | Loại | Đơn vào `sales_order.csv` / DB? | Hành động được gợi ý |
|---|---|---|---|
| `GARBLED_CUSTOMER_NAME` | Hard reject | ❌ | Liên hệ đại lý gửi lại email đúng format (body bị fallback PDF garbled) |
| `SELLER_AS_CUSTOMER` | Hard reject | ❌ | Tên trong body trùng Thống Nhất (seller) — xác minh ai là KH thật |
| `NEW_MST` | Soft warning | ✅ | MST chưa có trong customer master — verify rồi tạo `KH-NEW-*` (tự động ở mode `--write-db`) |
| `UNKNOWN_PRODUCT(n)` | Soft warning | ✅ | `n` product_code chưa có trong SKU master — phối hợp phòng SP map vào `product_line`, set `is_active=TRUE` |

Threshold mặc định **10%**: nếu flag nào vượt threshold, pipeline log warning cuối run đề xuất cán bộ xử lý batch thay vì từng đơn lẻ. Constant `WARN_THRESHOLD` trong `src/ingest/cli.py`.

Trên dataset T3/2026:
- `NEW_MST = 20.7%` → vượt → cán bộ cần onboard 96 đại lý mới
- `UNKNOWN_PRODUCT = 6.9%` → dưới threshold → handle từng đơn

Chi tiết logic + 5 vấn đề + giải pháp thay thế: [`docs/data-quality.md`](docs/data-quality.md).

---

## 9. Load vào Postgres

### Cách 1 — `--write-db` (recommended, 1 lệnh)

```bash
set -a; source .env; set +a
uv run ingest --source-dir "Emails & Files" --out output/ --write-db
```

Đây là cách dùng cho Hạng mục A.4 (yêu cầu "ghi vào database tự động"). Idempotent, an toàn cho rerun.

### Cách 2 — SQL thủ công (cho khi không bật DB extra)

Dùng khi không cài `psycopg` (mode CSV thuần ở mục 6.1/6.2). Chạy block SQL sau với `psql`:

```sql
SET search_path TO tnbike, public;
BEGIN;

-- 1) Staging tables
CREATE TEMP TABLE stage_so (
  so_number          VARCHAR(20),
  invoice_symbol     VARCHAR(15),
  invoice_number     VARCHAR(20),
  order_date         DATE,
  customer_code      VARCHAR(20),
  customer_tax_code  VARCHAR(15),
  customer_name      TEXT,
  customer_address   TEXT,
  total_amount       NUMERIC(15,2),
  total_quantity     INTEGER,
  line_count         INTEGER
);
\copy stage_so FROM 'output/sales_order.csv' WITH CSV HEADER;

CREATE TEMP TABLE stage_ol (
  so_number VARCHAR(20), line_no INTEGER, product_code VARCHAR(20),
  product_name TEXT, unit VARCHAR(20), quantity NUMERIC(10,2),
  unit_price NUMERIC(15,2), line_total NUMERIC(15,2)
);
\copy stage_ol FROM 'output/order_line.csv' WITH CSV HEADER;

-- 2) Tạo KH-NEW-* cho MST mới
INSERT INTO customer (customer_code, customer_name, tax_code, address)
SELECT
  'KH-NEW-' || LPAD(ROW_NUMBER() OVER (ORDER BY customer_tax_code)::TEXT, 5, '0'),
  customer_name, customer_tax_code, NULLIF(customer_address, '')
FROM (
  SELECT DISTINCT ON (customer_tax_code) customer_tax_code, customer_name, customer_address
  FROM stage_so s
  WHERE NOT EXISTS (SELECT 1 FROM customer c WHERE c.tax_code = s.customer_tax_code)
  ORDER BY customer_tax_code, customer_name
) new_kh;

-- 3) Tạo SKU placeholder cho product_code mới (is_active=FALSE)
INSERT INTO product (product_code, product_name, line_id, color, unit, is_active)
SELECT DISTINCT ON (l.product_code) l.product_code, l.product_name, NULL, NULL, 'Chiếc', FALSE
FROM stage_ol l
WHERE NOT EXISTS (SELECT 1 FROM product p WHERE p.product_code = l.product_code)
ON CONFLICT (product_code) DO NOTHING;

-- 4) sales_order
INSERT INTO sales_order
  (so_number, invoice_symbol, invoice_number, order_date, customer_code,
   total_amount, total_quantity, line_count)
SELECT s.so_number, NULLIF(s.invoice_symbol,''), NULLIF(s.invoice_number,''),
       s.order_date, c.customer_code, s.total_amount, s.total_quantity, s.line_count
FROM stage_so s
JOIN customer c ON c.tax_code = s.customer_tax_code;

-- 5) order_line (trigger trg_order_line_after_insert tự refresh totals)
INSERT INTO order_line (order_id, so_number, product_code, quantity, unit_price, line_total)
SELECT so.order_id, l.so_number, l.product_code, l.quantity, l.unit_price, l.line_total
FROM stage_ol l
JOIN sales_order so ON so.so_number = l.so_number;

-- 6) email_log
\copy email_log (message_id, from_address, received_at, attachment_name, so_number, processing_status, error_message) FROM 'output/email_log.csv' WITH CSV HEADER;

-- 7) Refresh fact_sales T3/2026
DELETE FROM fact_sales WHERE fiscal_year = 2026 AND fiscal_month = 3;
INSERT INTO fact_sales (
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
FROM order_line ol
JOIN sales_order so USING (order_id)
JOIN product p ON p.product_code = ol.product_code
LEFT JOIN product_line pl ON pl.line_id = p.line_id
LEFT JOIN product_group pg ON pg.group_code = pl.group_code
LEFT JOIN customer c ON c.customer_code = so.customer_code
LEFT JOIN province pr ON pr.province_id = c.province_id
WHERE so.fiscal_year = 2026 AND so.fiscal_month = 3;

COMMIT;
```

---

## 10. Test

```bash
uv run pytest -v
```

| Test file | Cover | Note |
|---|---|---|
| `tests/test_customer_extractor.py` | 6 case: 3 format email body (`Khách hàng:` / `Tên:` / `Đại lý:`) + empty body + `is_garbled_name` + `is_seller_name` | Deterministic, không cần dataset |
| `tests/test_pdf_parser.py` | 3 smoke test trên `BH26_0935.pdf` | Skip nếu không có dataset local (path hard-code `data/Emails & Files/`) |

Lint:

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

---

## 11. Cấu trúc thư mục

```
data-explorers-2026/
├── pyproject.toml                  # uv project + entry point `ingest`
├── requirements.txt                # mirror cho user pip
├── uv.lock                         # lockfile deterministic
├── .python-version                 # 3.12
├── .env.example                    # template DATABASE_URL + PG* env vars
├── .gitignore                      # exclude dataset BTC, output/, .env
├── README.md
├── src/ingest/
│   ├── __init__.py
│   ├── cli.py                      # entry: `ingest`, orchestrator, CSV/Excel writer
│   ├── email_parser.py             # parse .eml → header + body + PDF bytes
│   ├── customer_extractor.py       # parse body UTF-8 → tên/MST/địa chỉ đại lý
│   ├── pdf_parser.py               # parse PDF → SalesOrder (header + lines)
│   ├── models.py                   # Pydantic: OrderLine / SalesOrder / EmailLog
│   ├── validators.py               # cross-check qty × price = line_total + sum lines
│   └── db_writer.py                # --write-db: ghi DB + refresh fact_sales
├── tests/
│   ├── test_customer_extractor.py  # 6 case cover 3 format + garbled/seller detectors
│   └── test_pdf_parser.py          # smoke test với BH26_0935.pdf
└── docs/
    └── data-quality.md             # 5 vấn đề data + logic + giải pháp thay thế
```

Folders chỉ có local, không commit:
- `Emails & Files/` — dataset BTC cấp
- `Database/` — DDL + dataset BTC
- `output/` — output CSV/Excel + db_export
- `.venv/`, `.env`, `__pycache__/`, `.pytest_cache/`

---

## 12. Lưu ý kỹ thuật

- **Decimal cho VND**: dùng `Decimal` toàn bộ, không `float`. Pipeline parse số VN format dot-comma chuẩn (`1.898.148,50` → `Decimal('1898148.50')`).
- **Format số VN**: chấm = thousand separator, phẩy = decimal. `pdf_parser._to_decimal()` handle cả 2 case.
- **MST Thống Nhất** (`0300397904`) skip khi tìm MST đại lý — xuất hiện ở header PDF của seller. Constant `TNH_TAX` trong `pdf_parser.py:18`.
- **SO number fallback**: nếu PDF text không match `BH\d{2}\.\d{4}`, dùng filename `BH26_0935.eml` → `BH26.0935` làm hint.
- **Trigger `trg_order_line_after_insert`** trong DB tự refresh `sales_order.total_amount / total_quantity / line_count` mỗi khi insert `order_line`. `db_writer` đảm bảo `SET search_path TO tnbike, public` để trigger fire đúng table.
- **`fact_sales` không có trigger tự sync** — `--write-db` DELETE + INSERT lại cho phạm vi T3/2026 mỗi lần chạy (idempotent).
- **Tolerance validator**: `max(±10đ absolute, ±0.001% line_total)`. Đủ buffer rounding noise của ReportLab nhưng vẫn bắt được lỗi data thực sự.
- **Idempotency của `--write-db`**: check `so_number` tồn tại trong `sales_order` → skip. `email_log` dùng `ON CONFLICT (message_id) DO NOTHING`. Có thể chạy lại pipeline an toàn.
- **Master cache**: pipeline đọc 1 lần đầu run (`customer.tax_code`, `product.product_code`) → in-memory set, O(1) lookup mỗi đơn.

---

## 13. Tài liệu liên quan

- [`docs/data-quality.md`](docs/data-quality.md) — 5 vấn đề data quality + logic xử lý hiện tại + 4–5 giải pháp thay thế mỗi vấn đề + decisions log.
- `Database/01_create_tables.sql` — DDL gốc BTC: 10 bảng + 4 view + 1 trigger.
- `Database/02_import_data.sql` — Dump baseline T1/2025–T2/2026 (17.031 lines).
- `Database/Tnbike_Database_Schema_Doc.docx` — Schema doc tiếng Việt từ BTC.
- `DataExplorers2026 - Đề thi Vòng 2.pdf` — Đề thi gốc (28/4/2026).
