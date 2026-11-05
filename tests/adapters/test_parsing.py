import io
import zipfile

import docx
import pytest

from editta.adapters.parsing import EXTRACTORS, parse_manuscript
from editta.adapters.parsing import pdf as pdf_module
from editta.adapters.parsing.safety import (
    MAX_UPLOAD_BYTES,
    ManuscriptFormat,
    sanitize_filename,
    validate_upload,
)
from editta.domain.errors import ParseError
from tests.adapters.pdf_factory import encrypted, text_pdf, with_outline


def _build_docx_bytes(paragraphs: list[str], heading: str | None = None) -> bytes:
    document = docx.Document()
    if heading:
        document.add_heading(heading, level=1)
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_validate_upload_accepts_real_docx() -> None:
    validate_upload(_build_docx_bytes(["hello"]), "essay.docx")


def test_validate_upload_rejects_empty_file() -> None:
    with pytest.raises(ParseError, match="empty file"):
        validate_upload(b"", "essay.docx")


def test_validate_upload_rejects_oversized_file() -> None:
    oversized = b"x" * (MAX_UPLOAD_BYTES + 1)
    with pytest.raises(ParseError, match="exceeds"):
        validate_upload(oversized, "essay.docx")


def test_validate_upload_rejects_macro_enabled_docm() -> None:
    with pytest.raises(ParseError, match="macro-enabled"):
        validate_upload(_build_docx_bytes(["hello"]), "essay.docm")


def test_validate_upload_rejects_extension_mismatch() -> None:
    with pytest.raises(ParseError, match="does not match"):
        validate_upload(b"not a real docx", "essay.docx")


def test_validate_upload_rejects_zip_bomb_entry_count() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(2001):
            archive.writestr(f"entry_{i}.xml", "x")
    with pytest.raises(ParseError, match="more than"):
        validate_upload(buffer.getvalue(), "essay.docx")


def test_validate_upload_rejects_path_traversal_entry() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../etc/passwd", "x")
    with pytest.raises(ParseError, match="unsafe archive entry path"):
        validate_upload(buffer.getvalue(), "essay.docx")


def test_validate_upload_rejects_unsupported_extension() -> None:
    with pytest.raises(ParseError, match="unsupported file type"):
        validate_upload(b"data", "essay.txt")


def test_parse_docx_extracts_text_and_sections() -> None:
    data = _build_docx_bytes(["first paragraph", "second paragraph"], heading="Chapter One")

    manuscript = parse_manuscript(data, "essay.docx")

    assert "first paragraph" in manuscript.text
    assert "second paragraph" in manuscript.text
    assert manuscript.sections == ("Chapter One",)
    assert manuscript.source_filename == "essay.docx"


def test_parse_docx_rejects_empty_document() -> None:
    with pytest.raises(ParseError, match="no extractable text"):
        parse_manuscript(_build_docx_bytes([]), "essay.docx")


# --- format dispatch, PDF, filename sanitizing --------------------------------------


def test_validate_upload_returns_detected_format() -> None:
    assert validate_upload(_build_docx_bytes(["x"]), "a.docx") is ManuscriptFormat.DOCX
    assert validate_upload(text_pdf([["x"]]), "a.pdf") is ManuscriptFormat.PDF


def test_every_accepted_format_has_an_extractor() -> None:
    assert set(EXTRACTORS) == set(ManuscriptFormat)


def test_parse_manuscript_dispatches_pdf_to_the_pdf_extractor() -> None:
    pdf = text_pdf([["First page line."], ["Second page line."]])

    manuscript = parse_manuscript(pdf, "essay.pdf")

    assert manuscript.text == "First page line.\nSecond page line."
    assert manuscript.source_filename == "essay.pdf"


def test_parse_manuscript_dispatches_docx_to_the_docx_extractor() -> None:
    manuscript = parse_manuscript(_build_docx_bytes(["hello"], heading="Ch"), "essay.docx")
    assert manuscript.sections == ("Ch",)


def test_parse_pdf_reads_sections_from_outline() -> None:
    pdf = with_outline(text_pdf([["Intro text."], ["Body text."]]), ["Intro", "Body"])

    assert parse_manuscript(pdf, "essay.pdf").sections == ("Intro", "Body")


def test_parse_pdf_collapses_inline_whitespace() -> None:
    pdf = text_pdf([["spaced    out     words"]])
    assert parse_manuscript(pdf, "essay.pdf").text == "spaced out words"


def test_parse_pdf_rejects_image_only_pdf() -> None:
    with pytest.raises(ParseError, match="no extractable text"):
        parse_manuscript(text_pdf([[]]), "scan.pdf")


def test_parse_pdf_rejects_encrypted_pdf() -> None:
    with pytest.raises(ParseError, match="encrypted"):
        parse_manuscript(encrypted(text_pdf([["secret"]])), "locked.pdf")


def test_parse_pdf_rejects_garbage_after_magic() -> None:
    with pytest.raises(ParseError, match="failed to"):
        parse_manuscript(b"%PDF-1.4 this is not a pdf body", "broken.pdf")


def test_parse_pdf_enforces_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_module, "MAX_PDF_PAGES", 1)
    with pytest.raises(ParseError, match="pages"):
        parse_manuscript(text_pdf([["a"], ["b"]]), "long.pdf")


def test_parse_pdf_enforces_extracted_text_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_module, "MAX_EXTRACTED_CHARS", 10)
    with pytest.raises(ParseError, match="maximum extractable size"):
        parse_manuscript(text_pdf([["more than ten characters"]]), "big.pdf")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("essay.docx", "essay.docx"),
        ("../../etc/passwd.docx", "passwd.docx"),
        ("C:\\Users\\me\\Δοκίμιο.docx", "Δοκίμιο.docx"),
        ("bad\x00name\x1f.pdf", "badname.pdf"),
        ("  lots   of   space .pdf ", "lots of space .pdf"),
        ("...hidden.docx", "hidden.docx"),
        ("", "manuscript"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_caps_length_and_keeps_extension() -> None:
    cleaned = sanitize_filename("α" * 500 + ".docx")
    assert len(cleaned) == 200 and cleaned.endswith(".docx")


def test_pdf_text_has_no_xml_forbidden_characters() -> None:
    """Broken font maps yield control characters; the DOCX export (XML 1.0) rejects them."""
    pdf = text_pdf([["Broken\x01font \x00words plain here\x1f."]])

    assert parse_manuscript(pdf, "essay.pdf").text == "Brokenfont words plain here."


def test_pdf_control_character_filter_keeps_ordinary_whitespace_and_text() -> None:
    text = parse_manuscript(text_pdf([["one\ttwo"], ["three"]]), "essay.pdf").text

    assert text == "one two\nthree"
