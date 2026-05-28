# Data Quality — Vấn đề gặp phải & logic xử lý

Tài liệu này liệt kê các vấn đề chất lượng dữ liệu pipeline đã phát hiện trên 1.132 đơn T3/2026, mô tả nguyên nhân gốc, logic xử lý hiện tại, và các giải pháp thay thế đã cân nhắc.

**Nguyên tắc xuyên suốt**: trung thành data gốc — pipeline **không sửa nội dung** mà chỉ flag, gợi ý hành động, hoặc tạo placeholder để cán bộ verify thủ công.

---

## Tổng quan kết quả

Trên 1.132 đơn T3/2026:

| Status | Số đơn | Tỷ lệ |
|---|---:|---:|
| OK | 850 | 75.1% |
| REVIEW_REQUIRED | 282 | 24.9% |
| _trong đó NEW_MST_ | 234 | 20.7% |
| _trong đó UNKNOWN_PRODUCT_ | 78 | 6.9% |
| PARSE_ERROR / NO_ATTACHMENT / VALIDATION_ERROR | 0 | 0% |

Sau khi `--write-db` (Phương án A đề bài, mode tự động):
- 1.132/1.132 đơn vào `sales_order` (full coverage)
- 8.723 lines vào `order_line` (full coverage, nhờ SKU placeholder)
- 8.723 rows vào `fact_sales` T3/2026 (full coverage)
- 96 KH-NEW-* tự tạo
- 18 SKU placeholder tự tạo (`is_active=FALSE`)

---

## 1. Vấn đề: Customer name bị garbled (font PDF)

### Symptom

Sample customer name trước khi fix:

```
CÔNG TY Cn PHnN XE nnP THnNG NHnT       (đúng: CÔNG TY CỔ PHẦN XE ĐẠP THỐNG NHẤT)
Xe nnp Thnng Nhnt MTB 26-02 Đỏ tươi      (đúng: Xe đạp Thống Nhất MTB 26-02 Đỏ tươi)
```

Khi load vào DB rồi đưa lên dashboard, các row này hiển thị tên KH/sản phẩm là chuỗi vô nghĩa "Thnng Nhnt".

### Root cause

PDF do ReportLab generate dùng font **Helvetica với WinAnsi encoding** — không có ToUnicode CMap đầy đủ cho Vietnamese diacritics. Cả `pdfplumber` lẫn `pypdfium2` đều render `Đặt`, `Phòng`, `Trống`, `Đ`, `ạ` thành các ký tự ASCII gần nhất như `n`, `■`, `Hnng`. Đây là **vấn đề ở nguồn PDF**, không thể sửa ở reader.

### Logic hiện tại

1. **Ưu tiên email body** (UTF-8 sạch) làm nguồn của customer info, override giá trị từ PDF (`cli.py:_process_one`).
2. Nếu body không match được pattern → flag `GARBLED_CUSTOMER_NAME` (hard reject).
3. Detector regex: `thnng|nhnt|cn ph[ae]n|nnp|hnng|■` (case-insensitive).

### Giải pháp thay thế

| Giải pháp | Ưu | Nhược |
|---|---|---|
| **(Đang dùng) Body là nguồn chính, PDF fallback + reject** | Đơn giản, không phụ thuộc service ngoài | Phụ thuộc body format của đại lý |
| Re-render PDF bằng font có ToUnicode CMap (ghostscript, mutool) | Sửa root cause | Cần workflow tiền xử lý, chậm, mỗi PDF ~1s |
| Dùng OCR (tesseract, easyocr) trên PDF render thành PNG | Đọc đúng diacritics | Chậm hơn 10–20× pdfplumber, độ chính xác phụ thuộc font size |
| LLM Vision (Claude Sonnet, GPT-4 Vision) decode PDF | Robust với nhiều layout | Chi phí cao, latency, không deterministic |
| Mapping bảng `garbled → đúng` thủ công | Deterministic | Phải maintain bảng dài, cố định cho 1 font version |

---

## 2. Vấn đề: Email body có nhiều format khác nhau

### Symptom

3 format thường gặp trong 1.132 emails:

**Format A** (`BH26_0936`):
```
Khách hàng   : CÔNG TY TNHH THƯƠNG MẠI LONG PHÚ
MST          : 167397253
```

**Format B** (`BH26_0944`):
```
Thông tin đại lý:
  Tên     : CÔNG TY CỔ PHẦN THƯƠNG MẠI VIỆT ANH
  MST     : 150425614
```

**Format C** (`BH26_0938`):
```
Đại lý   : CÔNG TY CỔ PHẦN NAM TIẾN
MST      : 111014028
```

Regex cũ chỉ match Format C → 35% đơn rớt về PDF fallback garbled.

### Root cause

Đại lý gửi email không có chuẩn template chung. Hệ thống ERP của Thống Nhất chưa enforce form input.

### Logic hiện tại

`customer_extractor._NAME_PATTERNS` thử 4 pattern theo thứ tự ưu tiên:
1. `Khách hàng :` (Format A)
2. `Đại lý :` (Format C) — yêu cầu có giá trị non-empty cùng dòng để tránh match nhầm "Thông tin đại lý:"
3. `Tên :` hoặc `Tên đại lý :` (Format B)
4. `Đơn vị :` (fallback)

Sau khi extract, `_clean_name` strip prefix `Tên :` nếu group capture còn dính (do `\s*` của regex sau dấu `:` ăn cả newline).

### Giải pháp thay thế

| Giải pháp | Ưu | Nhược |
|---|---|---|
| **(Đang dùng) Multi-pattern fallback** | Robust với 3 format đã thấy | Cần update khi gặp format mới |
| Trích bằng LLM ("trích tên KH từ body này") | Tự thích nghi với mọi format | Cost, không deterministic, phải prompt cache |
| Ép đại lý dùng form web → JSON | Chuẩn hóa nguồn | Cần thay đổi quy trình kinh doanh, không trong tầm dev |
| ML / NER (vietnamese-NER, underthesea) | Học pattern từ data | Train data ít, không chắc tốt hơn regex cho VN text |
| Bắt buộc đại lý ghi đầu email "Đại lý: ..." | Đơn giản | Đại lý không hợp tác → fallback PDF như cũ |

---

## 3. Vấn đề: MST không có trong customer master (NEW_MST)

### Symptom

234/1.132 đơn (20.7%) có `customer_tax_code` không tồn tại trong `tnbike.customer` 702 KH. User cảnh báo "200+ đơn lỗi MST không match".

### Root cause

Customer master 702 KH là baseline T1/2025 — T2/2026 từ BTC. T3/2026 xuất hiện 96 đại lý mới (đại lý lần đầu đặt hàng) → MST chưa có. Không phải lỗi data, mà là tín hiệu **business event** — onboarding KH mới.

### Logic hiện tại

1. Pipeline flag `NEW_MST` (soft warning — đơn vẫn xuất vào `sales_order.csv`).
2. `--write-db` mode: tự `INSERT INTO customer (customer_code, ...) VALUES ('KH-NEW-NNNNN', name, tax_code, address)`. 96 customer mới được tạo từ body UTF-8 sạch.
3. Cuối run, log warning nếu vượt threshold 10% — gợi ý cán bộ liên hệ kiểm tra danh sách đại lý mới này có hợp lệ không.

### Giải pháp thay thế

| Giải pháp | Ưu | Nhược |
|---|---|---|
| **(Đang dùng) Auto-create KH-NEW-* + cán bộ verify sau** | Đơn được ghi nhận đầy đủ ngay, không block flow | Có thể tạo duplicate nếu cùng KH gửi sai MST 2 lần |
| Reject hard (đơn không vào DB) | An toàn | Mất 21% doanh số T3 — không acceptable |
| Lookup MST qua API GDT (`gdt.gov.vn`) | Tự verify tên ứng MST đúng | Cần API key, rate limit, slow |
| Fuzzy match name → KH cũ trùng tên (probabilistic) | Tránh duplicate | Risk: gán nhầm vào KH cũ khác |
| Tạo state "pending_customer" + queue cán bộ approve | Kiểm soát chặt | Cần build UI workflow, vượt scope |

---

## 4. Vấn đề: Product_code không có trong product master (UNKNOWN_PRODUCT)

### Symptom

78 đơn có ít nhất 1 line dùng `product_code` không tồn tại trong `tnbike.product` 247 SKU. 81 lines bị ảnh hưởng. Tất cả tên product này đều garbled từ PDF (vd `Xe nnp Thnng Nhnt unite 20`).

### Root cause

Product master 247 SKU thiếu một số mã mới release T3/2026 (chưa onboard vào hệ thống ERP). Không thể tra ngược tên đúng từ PDF garbled.

### Logic hiện tại

1. Pipeline flag `UNKNOWN_PRODUCT(n)` (soft warning — n = số SKU lạ trong đơn).
2. `--write-db` mode: tự `INSERT INTO product (product_code, product_name, line_id, color, unit, is_active) VALUES (code, garbled_name, NULL, NULL, 'Chiếc', FALSE)`.
   - `line_id=NULL` → SKU chưa map vào dòng/nhóm sản phẩm.
   - `is_active=FALSE` → phòng SP biết SKU này chờ verify, không xuất hiện trong báo cáo bán hàng đang hoạt động.
   - `product_name = garbled` → giữ nguyên text từ PDF, cán bộ phòng SP update sau khi tra cứu nội bộ.
3. `order_line` insert được full → `fact_sales` cũng full (LEFT JOIN product_line / product_group cho phép `line_name = NULL`).

### Giải pháp thay thế

| Giải pháp | Ưu | Nhược |
|---|---|---|
| **(Đang dùng) Auto-create SKU-NEW placeholder (is_active=FALSE) + cán bộ map sau** | Đơn full coverage, fact_sales đầy đủ ngay | SKU placeholder có tên garbled — báo cáo chi tiết SKU phải filter `is_active=TRUE` |
| Skip line, đơn không có line nào → mất doanh số | An toàn | Mất 91/1132 đơn (7%) khỏi fact_sales |
| Dùng `pypdf` thay vì `pdfplumber` để đọc lại product_name | Có thể đọc đúng ở vài PDF | Cùng vấn đề font → không hiệu quả |
| Re-render PDF (mutool / ghostscript) rồi parse | Sửa root cause | Build pipeline tiền xử lý nặng |
| Tách order_line riêng "staging_order_line" để phòng SP review | Tách concern rõ | Cần thêm bảng + workflow UI |

---

## 5. Vấn đề: Seller (Thống Nhất) bị parse nhầm thành customer

### Symptom

Trước khi mở rộng regex, 395 đơn (35%) có `customer_name = "CÔNG TY Cn PHnN XE nnP THnNG NHnT"` — đây là tên seller (Thống Nhất) garbled, không phải đại lý.

### Root cause

Email body không match được pattern → pipeline fallback đọc PDF header → match được dòng "CÔNG TY CỔ PHẦN XE ĐẠP THỐNG NHẤT" ở vị trí seller info, hiểu nhầm thành đại lý.

### Logic hiện tại

1. Mở rộng regex body (xem mục 2) → trường hợp này không còn xảy ra trên dataset T3/2026.
2. Phòng hờ: thêm flag `SELLER_AS_CUSTOMER` — nếu sau extract mà tên match "Thống Nhất" (sau khi normalize Unicode) → hard reject. `is_seller_name()` trong `customer_extractor.py`.
3. Trong PDF parser: skip MST `0300397904` (MST của Thống Nhất) khi tìm MST đại lý (constant `TNH_TAX` trong `pdf_parser.py`).

### Giải pháp thay thế

| Giải pháp | Ưu | Nhược |
|---|---|---|
| **(Đang dùng) Multi-layer defense: mở rộng regex + seller detector + MST skip** | Triệt tiêu hoàn toàn trên dataset hiện tại | Nếu seller info thay đổi (vd đổi tên công ty) → phải update regex |
| Hard-code blacklist tax_code = "0300397904" trong loader | Đơn giản | Không catch trường hợp seller có MST khác (vd subsidiary) |
| Whitelist KH master, reject mọi tên không match | An toàn cứng | Block toàn bộ KH mới (mục 3 mâu thuẫn) |
| Phân tích vị trí trong PDF (header vs body) bằng layout analysis | Robust | Phức tạp, cần ML / heuristic ổn định |

---

## 6. Threshold và batch action

`WARN_THRESHOLD = 0.10` trong `cli.py`. Khi tỷ lệ flag nào vượt ngưỡng, pipeline log warning cuối run gợi ý cán bộ vào xử lý batch thay vì từng đơn lẻ.

Hiện tại:
- `NEW_MST = 20.7%` → vượt → đề xuất onboarding 96 đại lý mới
- `UNKNOWN_PRODUCT = 6.9%` → dưới ngưỡng → chỉ flag riêng, không batch

Tinh chỉnh threshold tùy ngữ cảnh (BTC, phòng kinh doanh) — chỉnh trong source, không qua flag CLI (chủ ý: không expose để user nghịch).

---

## 7. Cách check + xử lý nhanh

```bash
# Source env để load DATABASE_URL
set -a; source .env; set +a

# Chạy pipeline đầy đủ + ghi DB tự động
uv run ingest --source-dir "Emails & Files" --out output/ --write-db

# Mở review_required.csv để xem đơn cần xử lý
column -ts',' output/review_required.csv | less -S

# Xem 96 customer mới + 18 SKU placeholder vừa tạo
psql -d tnbike_db -c "
  SELECT customer_code, customer_name, tax_code
  FROM tnbike.customer
  WHERE customer_code LIKE 'KH-NEW-%' ORDER BY customer_code;
"
psql -d tnbike_db -c "
  SELECT product_code, product_name
  FROM tnbike.product
  WHERE is_active = FALSE ORDER BY product_code;
"
```

---

## 8. Decisions log (đã thống nhất với user)

| # | Decision | Lý do chọn |
|---|---|---|
| 1 | UNKNOWN_PRODUCT → auto-create SKU placeholder | Đảm bảo full coverage fact_sales T3/2026, đúng spec đề bài A.4 |
| 2 | Đơn đã có so_number → skip (idempotent) | An toàn cho rerun pipeline dev/debug |
| 3 | `--write-db` ghi cả DB + CSV (default khi flag bật) | CSV là source of truth audit, DB là consumption |
| 4 | `--write-db` tự refresh fact_sales T3/2026 | Đúng spec A.4 "sau khi ghi xong fact_sales phải được cập nhật" |
| 5 | Threshold 10% cho warning | Heuristic — tỷ lệ này đủ cao để cán bộ phải intervene |
| 6 | KH-NEW-* / SKU-NEW-* naming pattern | Khớp template SQL trong README cũ, dễ filter |
| 7 | Hard reject GARBLED + SELLER, soft warning NEW_MST + UNKNOWN_PRODUCT | Garbled/seller là lỗi parsing rõ ràng; new_mst/unknown_product là business event |
