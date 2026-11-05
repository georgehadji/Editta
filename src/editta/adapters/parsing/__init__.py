"""DOCX/PDF parsing: untrusted-input handling.

`parse_manuscript` is the one entrypoint the application uses. It validates once,
then dispatches on the *detected* format through a registry (Strategy): adding a
format means one extractor and one registry entry, with no caller changes.
"""

from collections.abc import Callable, Mapping
from types import MappingProxyType

from editta.adapters.parsing.docx import extract_docx
from editta.adapters.parsing.pdf import extract_pdf
from editta.adapters.parsing.safety import ManuscriptFormat, sanitize_filename, validate_upload
from editta.domain.models import ManuscriptText

Extractor = Callable[[bytes, str], ManuscriptText]

EXTRACTORS: Mapping[ManuscriptFormat, Extractor] = MappingProxyType(
    {
        ManuscriptFormat.DOCX: extract_docx,
        ManuscriptFormat.PDF: extract_pdf,
    }
)


def parse_manuscript(data: bytes, filename: str) -> ManuscriptText:
    manuscript_format = validate_upload(data, filename)
    return EXTRACTORS[manuscript_format](data, filename)


__all__ = [
    "EXTRACTORS",
    "ManuscriptFormat",
    "parse_manuscript",
    "sanitize_filename",
    "validate_upload",
]
