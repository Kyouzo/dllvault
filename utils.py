"""Small, dependency-free helpers for untrusted input."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote, unquote

MAX_NAME_LENGTH = 180
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_UNSAFE_DISPLAY = re.compile(r'[<>:"/\\|?*]')
_ASCII_SAFE = re.compile(r"[^A-Za-z0-9._ -]")
_MIME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]{0,63}/[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]{0,126}$")
FILE_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")


def sanitize_filename(raw: str | None) -> str:
    """Return a display-safe filename. Never used as a filesystem path."""
    name = unicodedata.normalize("NFC", raw or "")
    # Drop any path a client may have sent ("C:\\fakepath\\x", "../../x").
    name = re.split(r"[/\\]", name)[-1]
    name = _CONTROL.sub("", name)
    name = _UNSAFE_DISPLAY.sub("_", name)
    name = name.strip().strip(".").strip()
    if not name:
        name = "file"
    if len(name) > MAX_NAME_LENGTH:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 16:
            name = stem[: MAX_NAME_LENGTH - len(ext) - 1] + "." + ext
        else:
            name = name[:MAX_NAME_LENGTH]
    return name


def ascii_fallback(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = _ASCII_SAFE.sub("_", ascii_name).strip() or "download"
    return ascii_name[:MAX_NAME_LENGTH]


def content_disposition(name: str) -> str:
    """RFC 6266 attachment header with an ASCII fallback and a UTF-8 name."""
    return f"attachment; filename=\"{ascii_fallback(name)}\"; filename*=UTF-8''{quote(name, safe='')}"


def encode_meta(value: str) -> str:
    """S3/R2 user metadata must be ASCII; percent-encode everything else."""
    return quote(value, safe="")


def decode_meta(value: str | None) -> str:
    return unquote(value or "")


def sanitize_mime(raw: str | None) -> str:
    """Client-declared type, kept for display only. Never trusted for serving."""
    value = (raw or "").split(";", 1)[0].strip().lower()
    return value if _MIME.match(value) else "application/octet-stream"


def is_valid_file_id(file_id: str) -> bool:
    return bool(FILE_ID.match(file_id or ""))
