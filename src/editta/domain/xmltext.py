"""Text that is safe to put in a Word file. Pure.

XML 1.0 forbids most control characters and lone surrogates, and lxml (so python-docx)
raises on them. Model output is not trusted to avoid them, so every exporter cleans what
the model wrote before it reaches a document.
"""

import re

_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def strip_xml_illegal(text: str) -> str:
    return _XML_ILLEGAL.sub("", text)
