"""PDF -> normalized text + section map (defensive, text-layer PDFs only).

Untrusted input: guard clauses first, then extraction. pypdf caps each stream's
decompressed size itself (75 MB per stream, pypdf >= 6); the limits here bound the
*total* — page count and extracted characters — which pypdf does not.

Scanned (image-only) PDFs have no text layer and are rejected with a clear error;
OCR is out of scope. Sections come from the PDF outline (bookmarks) when present —
PDFs carry no reliable heading styles to recover them from otherwise.
"""

import io
import re

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from editta.domain.errors import ParseError
from editta.domain.models import ManuscriptText

MAX_PDF_PAGES = 2_000
MAX_EXTRACTED_CHARS = 5_000_000  # ~1M words: far past any essay/book manuscript

# Characters XML 1.0 forbids. Broken font maps put them in extracted text; they would
# otherwise reach the prompts, be quoted back, and make the DOCX export raise.
_XML_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

_INLINE_WHITESPACE = re.compile(r"[ \t ]+")


def extract_pdf(data: bytes, filename: str) -> ManuscriptText:
    """Extraction only. Callers must have run validate_upload (parse_manuscript does)."""
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except (PdfReadError, ValueError, OSError) as exc:
        raise ParseError(f"failed to parse .pdf: {exc}") from exc

    if reader.is_encrypted:
        raise ParseError("encrypted or password-protected PDFs are not accepted")
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        raise ParseError(f"failed to read .pdf page tree: {exc}") from exc
    if page_count > MAX_PDF_PAGES:
        raise ParseError(f"PDF has {page_count} pages; the limit is {MAX_PDF_PAGES}")

    lines: list[str] = []
    total_chars = 0
    for index, page in enumerate(reader.pages):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            raise ParseError(f"failed to extract text from PDF page {index + 1}: {exc}") from exc
        for raw_line in page_text.splitlines():
            line = _INLINE_WHITESPACE.sub(" ", _XML_FORBIDDEN.sub("", raw_line)).strip()
            if not line:
                continue
            total_chars += len(line)
            if total_chars > MAX_EXTRACTED_CHARS:
                raise ParseError("PDF text exceeds the maximum extractable size")
            lines.append(line)

    full_text = "\n".join(lines)
    if not full_text.strip():
        raise ParseError("PDF contains no extractable text (scanned PDFs are not supported)")

    return ManuscriptText(
        source_filename=filename, text=full_text, sections=_outline_titles(reader)
    )


def _outline_titles(reader: PdfReader) -> tuple[str, ...]:
    """Top-level bookmark titles. Best-effort: a malformed outline yields no sections
    rather than failing a document whose text extracted fine."""
    try:
        outline = reader.outline
    except Exception:
        return ()
    titles: list[str] = []
    for item in outline:
        if isinstance(item, list):  # nested children of the previous entry
            continue
        title = getattr(item, "title", None)
        if isinstance(title, str) and title.strip():
            titles.append(title.strip())
    return tuple(titles)
