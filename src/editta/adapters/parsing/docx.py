"""DOCX -> normalized text + section map."""

import io

import docx
from docx.document import Document
from docx.text.paragraph import Paragraph

from editta.domain.errors import ParseError
from editta.domain.models import ManuscriptText

_HEADING_PREFIX = "Heading"


def docx_body_paragraphs(document: Document) -> list[Paragraph]:
    """The paragraphs the analysis reads: top-level, with text. One walk for the parser and
    for the commented export, so the offsets of one are the offsets of the other."""
    return [p for p in document.paragraphs if p.text.strip()]


def extract_docx(data: bytes, filename: str) -> ManuscriptText:
    """Extraction only. Callers must have run validate_upload (parse_manuscript does)."""
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ParseError(f"failed to parse .docx: {exc}") from exc

    lines: list[str] = []
    sections: list[str] = []
    for paragraph in docx_body_paragraphs(document):
        text = paragraph.text.strip()
        style_name = paragraph.style.name if paragraph.style else ""
        if style_name.startswith(_HEADING_PREFIX):
            sections.append(text)
        lines.append(text)

    full_text = "\n".join(lines)
    if not full_text.strip():
        raise ParseError("document contains no extractable text")

    return ManuscriptText(source_filename=filename, text=full_text, sections=tuple(sections))
