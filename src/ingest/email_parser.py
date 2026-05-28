"""Parse .eml: header (From / Date / Subject / Message-ID) + tách PDF attachment."""

from __future__ import annotations

import email
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import NamedTuple


class ParsedEmail(NamedTuple):
    message_id: str
    from_address: str
    from_name: str
    subject: str
    received_at: datetime
    body_text: str
    attachment_name: str | None
    attachment_bytes: bytes | None


def parse_eml(path: Path) -> ParsedEmail:
    """Đọc 1 file .eml, decode header và tách PDF đính kèm đầu tiên (nếu có)."""

    with path.open("rb") as fp:
        msg: EmailMessage = email.message_from_binary_file(fp, policy=policy.default)  # type: ignore[assignment]

    from_name, from_addr = "", ""
    raw_from = msg.get("From", "")
    if raw_from:
        addrs = getaddresses([raw_from])
        if addrs:
            from_name, from_addr = addrs[0]

    body_text = _extract_body(msg)

    attachment_name: str | None = None
    attachment_bytes: bytes | None = None
    for part in msg.iter_attachments():
        ctype = part.get_content_type()
        fname = part.get_filename() or ""
        if ctype == "application/pdf" or fname.lower().endswith(".pdf"):
            attachment_name = fname
            payload = part.get_content()
            # get_content() trả str cho text part, bytes cho binary; với PDF luôn là bytes
            attachment_bytes = payload if isinstance(payload, bytes) else payload.encode()
            break

    return ParsedEmail(
        message_id=(msg.get("Message-ID", "") or "").strip("<>") or path.stem,
        from_address=from_addr,
        from_name=from_name,
        subject=msg.get("Subject", "") or "",
        received_at=_parse_date(msg.get("Date")),
        body_text=body_text,
        attachment_name=attachment_name,
        attachment_bytes=attachment_bytes,
    )


def _extract_body(msg: EmailMessage) -> str:
    """Lấy text/plain part đầu tiên — đã được policy.default tự decode UTF-8."""

    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        return ""
    payload = body_part.get_content()
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def _parse_date(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
