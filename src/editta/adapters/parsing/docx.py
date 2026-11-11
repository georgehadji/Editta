"""DOCX -> normalized text + section map."""

import io
from typing import Any

import docx
from docx.document import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from editta.domain.errors import ParseError
from editta.domain.models import ManuscriptText

_HEADING_PREFIX = "Heading"


def _outside_text_boxes(tag: str, root: Any) -> list[Any]:
    """Elements `tag` under `root` in document order, minus those inside a text box. A text
    box holds its own paragraphs, nested in the one that anchors it: reading those too
    would count the same words twice. (Plain iteration, not XPath: elements python-docx
    did not build, such as a comment range it just added, cannot resolve `w:` themselves.)"""
    box = qn("w:txbxContent")
    return [e for e in root.iter(qn(tag)) if next(e.iterancestors(box), None) is None]


def run_elements(element: Any) -> list[Any]:
    """Every run under `element`, in order, wherever Word nested it: directly, or inside a
    tracked insertion, a content control, a field, a hyperlink or a smart tag."""
    return _outside_text_boxes("w:r", element)


def paragraph_text(paragraph: Paragraph) -> str:
    """The paragraph's text as the author sees it: tracked insertions count, tracked
    deletions (`w:delText`) do not. python-docx's own `Paragraph.text` skips everything
    that is not a direct run or hyperlink, so a document typed under Track Changes or
    filled through content controls would read as nearly empty."""
    return "".join(run.text for run in run_elements(paragraph._p))


def docx_body_paragraphs(document: Document) -> list[Paragraph]:
    """The paragraphs the analysis reads: body and table cells, in document order, with
    text. One walk for the parser and for the commented export, so the offsets of one are
    the offsets of the other."""
    body = document._body
    return [
        paragraph
        for element in _outside_text_boxes("w:p", document.element.body)
        if paragraph_text(paragraph := Paragraph(element, body)).strip()
    ]


def extract_docx(data: bytes, filename: str) -> ManuscriptText:
    """Extraction only. Callers must have run validate_upload (parse_manuscript does)."""
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ParseError(f"failed to parse .docx: {exc}") from exc

    lines: list[str] = []
    sections: list[str] = []
    for paragraph in docx_body_paragraphs(document):
        text = paragraph_text(paragraph).strip()
        style_name = paragraph.style.name if paragraph.style else ""
        if style_name.startswith(_HEADING_PREFIX):
            sections.append(text)
        lines.append(text)

    full_text = "\n".join(lines)
    if not full_text.strip():
        raise ParseError("document contains no extractable text")

    return ManuscriptText(source_filename=filename, text=full_text, sections=tuple(sections))
