import io
import zipfile

import docx
import pytest

from editta.adapters.parsing.docx import parse_docx
from editta.adapters.parsing.safety import MAX_UPLOAD_BYTES, validate_upload
from editta.domain.errors import ParseError


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

    manuscript = parse_docx(data, "essay.docx")

    assert "first paragraph" in manuscript.text
    assert "second paragraph" in manuscript.text
    assert manuscript.sections == ("Chapter One",)
    assert manuscript.source_filename == "essay.docx"


def test_parse_docx_rejects_empty_document() -> None:
    with pytest.raises(ParseError, match="no extractable text"):
        parse_docx(_build_docx_bytes([]), "essay.docx")
