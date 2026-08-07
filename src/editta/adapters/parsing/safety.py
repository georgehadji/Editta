"""Upload hardening: size cap, magic-byte MIME check, zip-bomb + path-traversal guards."""

import io
import zipfile

from editta.domain.errors import ParseError

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000

_DOCX_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF"


def validate_upload(data: bytes, filename: str) -> None:
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
    elif lower.endswith(".pdf"):
        if not data.startswith(_PDF_MAGIC):
            raise ParseError("file extension .pdf does not match file content")
    else:
        raise ParseError(f"unsupported file type: {filename}")


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
