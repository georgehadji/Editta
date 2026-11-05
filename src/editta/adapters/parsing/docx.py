"""DOCX -> normalized text + section map."""

import io

import docx

from editta.adapters.parsing.safety import validate_upload
from editta.domain.errors import ParseError
from editta.domain.models import ManuscriptText

_HEADING_PREFIX = "Heading"


def parse_docx(data: bytes, filename: str) -> ManuscriptText:
    validate_upload(data, filename)
    return extract_docx(data, filename)


def extract_docx(data: bytes, filename: str) -> ManuscriptText:
    """Extraction only. Callers must have run validate_upload (parse_docx does)."""
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ParseError(f"failed to parse .docx: {exc}") from exc

    lines: list[str] = []
    sections: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style_name = paragraph.style.name if paragraph.style else ""
        if style_name.startswith(_HEADING_PREFIX):
            sections.append(text)
        lines.append(text)

    full_text = "\n".join(lines)
    if not full_text.strip():
        raise ParseError("document contains no extractable text")

    return ManuscriptText(source_filename=filename, text=full_text, sections=tuple(sections))
