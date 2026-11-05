"""Quote anchoring: every finding an editor sees must be verbatim-traceable to the source.

Mitigates both prompt-injection-steered and hallucinated findings (PLAN.md §5). Applied
to every analysis pass's findings. Synthesis never produces findings of its own — it
cites these anchored ones by id (domain/synthesis.py) — so this is the single gate.

What "anchored" means: the quote identifies one passage of the manuscript.

  * Long quotes (>= FUZZY_MIN_CHARS) may match approximately (partial ratio >= 0.9):
    models occasionally normalize a quotation mark or drop a diacritic, and a long
    near-match still points at one place.
  * Short quotes must match exactly AND occur exactly once. Fuzzy matching is
    meaningless for them — a ten-character string is 90% similar to countless
    substrings of a book — and a word that occurs 200 times does not locate anything.
    A single misspelled word still anchors, because typos are almost always unique.

Whitespace is collapsed and text NFC-normalized on both sides first: PDF extraction
breaks lines mid-sentence, and Greek text mixes precomposed and combining accents.
"""

import re
import unicodedata
from collections.abc import Iterable

from rapidfuzz import fuzz

from editta.domain.models import Finding

QUOTE_MATCH_THRESHOLD = 0.9
FUZZY_MIN_CHARS = 24

_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def is_quote_anchored(
    quote: str, source_text: str, *, threshold: float = QUOTE_MATCH_THRESHOLD
) -> bool:
    return _anchored(normalize_text(quote), normalize_text(source_text), threshold)


def anchor_findings(findings: Iterable[Finding], source_text: str) -> list[Finding]:
    """Keep only findings whose quote identifies a passage of the source text."""
    source = normalize_text(source_text)  # once per pass, not once per finding
    return [
        f for f in findings if _anchored(normalize_text(f.quote), source, QUOTE_MATCH_THRESHOLD)
    ]


def _anchored(quote: str, source: str, threshold: float) -> bool:
    if not quote:
        return False
    if len(quote) < FUZZY_MIN_CHARS:
        return source.count(quote) == 1
    return fuzz.partial_ratio(quote, source) / 100 >= threshold
