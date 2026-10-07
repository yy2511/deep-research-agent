"""File identity and optional verbatim text, independent of parsing support."""
from __future__ import annotations

import codecs
import re


def file_type(data: bytes, content_type: str = "") -> str:
    signatures = ((b"%PDF-", "application/pdf"), (b"\x89PNG\r\n\x1a\n", "image/png"),
                  (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"),
                  (b"PK\x03\x04", "application/zip"), (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),
                  (b"\x1f\x8b", "application/gzip"), (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"))
    for prefix, kind in signatures:
        if data.startswith(prefix):
            return kind
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return content_type.split(";", 1)[0].strip().lower() or "application/octet-stream"


def verbatim_text(data: bytes, content_type: str = "") -> tuple[str | None, str | None]:
    """Decode exactly when possible; never turn binary container bytes into prose."""
    kind = file_type(data, content_type)
    if (kind.startswith(("image/", "audio/", "video/")) or kind in (
            "application/pdf", "application/zip", "application/x-ole-storage",
            "application/gzip", "application/x-7z-compressed")):
        return None, None
    charset = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.I)
    encoding = charset.group(1) if charset else "utf-8"
    if data.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    try:
        text = data.decode(encoding, errors="strict")
    except (UnicodeError, LookupError):
        return None, None
    return (text, encoding) if text.strip() and "\x00" not in text else (None, None)
