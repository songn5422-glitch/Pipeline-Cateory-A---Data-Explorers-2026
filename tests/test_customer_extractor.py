"""Regression tests cho customer_extractor — đảm bảo 3 format body đều extract đúng tên KH."""

from __future__ import annotations

from ingest.customer_extractor import (
    extract_customer,
    is_garbled_name,
    is_seller_name,
)


BODY_FORMAT_A = """Kính gửi quý công ty,

  Số chứng từ  : BH26.0936
  Khách hàng   : CÔNG TY TNHH THƯƠNG MẠI LONG PHÚ
  MST          : 167397253
  Địa chỉ      : phố Tràng Thi, phường Hàng Trống, Quận Hoàn Kiếm, TP Hà Nội
"""

BODY_FORMAT_B = """Đính kèm đơn đặt hàng BH26.0944 ngày 01/03/2026.

Thông tin đại lý:
  Tên     : CÔNG TY CỔ PHẦN THƯƠNG MẠI VIỆT ANH
  MST     : 150425614
  Địa chỉ : Xã Hồng Sơn, Huyện Mỹ Đức, Hà Nội
  Tel     : 0992098027
"""

BODY_FORMAT_C = """Kính gửi quý công ty, Đơn hàng BH26.0938 ngày 01/03/2026:

  Đại lý   : CÔNG TY CỔ PHẦN NAM TIẾN
  MST      : 111014028
  Địa chỉ  : Phường Phú Diễn, TP Hà Nội
  Liên hệ  : 0804282252
"""


def test_format_a_khach_hang():
    info = extract_customer(BODY_FORMAT_A)
    assert info.name == "CÔNG TY TNHH THƯƠNG MẠI LONG PHÚ"
    assert info.tax_code == "167397253"
    assert info.address.startswith("phố Tràng Thi")


def test_format_b_thong_tin_dai_ly_then_ten():
    info = extract_customer(BODY_FORMAT_B)
    # Phải strip prefix "Tên :" — KH thật là VIỆT ANH, không phải "Tên : VIỆT ANH"
    assert info.name == "CÔNG TY CỔ PHẦN THƯƠNG MẠI VIỆT ANH"
    assert info.tax_code == "150425614"
    assert info.phone == "0992098027"


def test_format_c_dai_ly():
    info = extract_customer(BODY_FORMAT_C)
    assert info.name == "CÔNG TY CỔ PHẦN NAM TIẾN"
    assert info.tax_code == "111014028"


def test_empty_body():
    info = extract_customer("")
    assert info.name is None
    assert info.tax_code is None


def test_is_garbled_name_detects_pdf_fallback():
    # Pattern thực tế từ PDF ReportLab thiếu ToUnicode CMap
    assert is_garbled_name("CÔNG TY Cn PHnN XE nnP THnNG NHnT")
    assert is_garbled_name("Xe nnp Thnng Nhnt MTB 26")
    assert not is_garbled_name("CÔNG TY CỔ PHẦN XE ĐẠP THỐNG NHẤT")
    assert not is_garbled_name(None)


def test_is_seller_name_detects_thong_nhat():
    # Sau khi normalize Unicode chuẩn, seller name match
    assert is_seller_name("CÔNG TY CỔ PHẦN XE ĐẠP THỐNG NHẤT")
    assert is_seller_name("Xe đạp Thống Nhất")
    assert not is_seller_name("CÔNG TY TNHH LONG PHÚ")
    assert not is_seller_name(None)
