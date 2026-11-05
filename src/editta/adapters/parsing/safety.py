"""Upload hardening: size cap, magic-byte MIME check, zip-bomb + path-traversal guards.

`validate_upload` is the single gate every manuscript passes through, and it returns
the *detected* format so callers dispatch on what the bytes are — never on what the
filename claims (a .docx extension on PDF bytes is rejected, not parsed as either).
"""

import io
import re
import unicodedata
import zipfile
from enum import StrEnum

from editta.domain.errors import ParseError

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000
MAX_FILENAME_CHARS = 200

_DOCX_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF"


class ManuscriptFormat(StrEnum):
    DOCX = "docx"
    PDF = "pdf"


def validate_upload(data: bytes, filename: str) -> ManuscriptFormat:
    if len(data) == 0:
        raise ParseError("empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ParseError(f"file exceeds {MAX_UPLOAD_BYTES} byte limit")

    lower = filename.lower()
    if lower.endswith(".docm"):
        raise ParseError("macro-enabled Word documents (.docm) are not accepted")

    if lower.endswith(".docx"):
        if not data.startswith(_DOCX_MAGIC):
            raise ParseError("file extension .docx does not match file content")
        _validate_zip_container(data)
        return ManuscriptFormat.DOCX
    if lower.endswith(".pdf"):
        if not data.startswith(_PDF_MAGIC):
            raise ParseError("file extension .pdf does not match file content")
        return ManuscriptFormat.PDF
    raise ParseError("unsupported file type: only .docx and .pdf are accepted")


def sanitize_filename(filename: str) -> str:
    """Display-safe original filename, stored only as DB metadata (PLAN.md §5).

    Drops any client-supplied directory part, control/format characters and leading
    dots; collapses whitespace; caps length while keeping the extension. Never used
    to build a storage key — keys are UUIDs.
    """
    base = re.split(r"[\\/]", filename)[-1]
    cleaned = "".join(
        ch for ch in unicodedata.normalize("NFC", base) if unicodedata.category(ch)[0] != "C"
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lstrip(".")
    if not cleaned:
        return "manuscript"
    if len(cleaned) <= MAX_FILENAME_CHARS:
        return cleaned
    stem, dot, ext = cleaned.rpartition(".")
    if dot and 0 < len(ext) <= 10:
        return f"{stem[: MAX_FILENAME_CHARS - len(ext) - 1]}.{ext}"
    return cleaned[:MAX_FILENAME_CHARS]


def _validate_zip_container(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise ParseError(f"archive has more than {MAX_ZIP_ENTRIES} entries")
            total_uncompressed = 0
            for info in infos:
                if ".." in info.filename or info.filename.startswith("/"):
                    raise ParseError(f"unsafe archive entry path: {info.filename}")
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_DECOMPRESSED_BYTES:
                    raise ParseError("archive exceeds maximum decompressed size (zip-bomb guard)")
    except zipfile.BadZipFile as exc:
        raise ParseError("corrupt or invalid .docx (not a valid zip container)") from exc
