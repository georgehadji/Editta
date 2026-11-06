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

Both sides are normalized first, folding what models routinely "correct" when they
quote: NFC (precomposed vs combining accents), whitespace, case, quotation marks
(« » “ ” „ → "), apostrophes, dashes, words hyphenated across PDF line breaks, and
punctuation at the quote's edges. Accents are deliberately NOT folded: a missing
accent is exactly what Greek typo findings point at ("κειμενο" vs "κείμενο"), and
folding it would turn a unique typo into a common word.
"""

import re
import unicodedata
from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from typing import NamedTuple

from rapidfuzz import fuzz

from editta.domain.models import Finding

QUOTE_MATCH_THRESHOLD = 0.9
FUZZY_MIN_CHARS = 24

_WHITESPACE = re.compile(r"\s+")
_LINE_BREAK_HYPHEN = re.compile(r"(\w)-\s*\n\s*(\w)")
_FOLD = str.maketrans(
    {
        "«": '"',
        "»": '"',
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "`": "'",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "\u00a0": " ",
    }
)
# Edge punctuation a quote may gain or lose: Latin and Greek (";" is the Greek question
# mark, "·" the ano teleia), quotes, ellipsis.
_EDGE_PUNCTUATION = " .,;:!?·\u0387\"'…()[]-"


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = _LINE_BREAK_HYPHEN.sub(r"\1\2", text)
    text = text.translate(_FOLD).casefold()
    return _WHITESPACE.sub(" ", text).strip()


def _normalize_quote(quote: str) -> str:
    return normalize_text(quote).strip(_EDGE_PUNCTUATION)


def is_quote_anchored(
    quote: str, source_text: str, *, threshold: float = QUOTE_MATCH_THRESHOLD
) -> bool:
    return _anchored(_normalize_quote(quote), normalize_text(source_text), threshold)


def anchor_findings(findings: Iterable[Finding], source_text: str) -> list[Finding]:
    """Keep only findings whose quote identifies a passage of the source text."""
    source = normalize_text(source_text)  # once per pass, not once per finding
    return [
        f for f in findings if _anchored(_normalize_quote(f.quote), source, QUOTE_MATCH_THRESHOLD)
    ]


def _anchored(quote: str, source: str, threshold: float) -> bool:
    return _locate(quote, source, threshold) is not None


def _locate(quote: str, source: str, threshold: float) -> tuple[int, int] | None:
    """Where in the (normalized) source the (normalized) quote is: [start, end), or None.
    The one rule behind both "anchored" and "placeable"."""
    if not quote:
        return None
    if len(quote) < FUZZY_MIN_CHARS:
        # Whole-word occurrences: "ναι" must not count the "ναι" inside "είναι".
        pattern = rf"(?<!\w){re.escape(quote)}(?!\w)"
        found = list(islice(re.finditer(pattern, source), 2))
        return found[0].span() if len(found) == 1 else None
    alignment = fuzz.partial_ratio_alignment(quote, source)
    if alignment is not None and alignment.score / 100 >= threshold:
        return alignment.dest_start, alignment.dest_end
    return None


@dataclass(frozen=True, slots=True)
class Span:
    """A stretch of the original text: [start, end) offsets."""

    start: int
    end: int


class MappedText(NamedTuple):
    """Normalized text plus, for every normalized character, the stretch of the original
    that produced it."""

    text: str
    starts: array[int]
    ends: array[int]

    def original_span(self, start: int, end: int) -> Span:
        return Span(self.starts[start], self.ends[end - 1])


def normalize_with_map(text: str) -> MappedText:
    """`normalize_text` that also says where each character came from. Same steps in the
    same order. Slower than `normalize_text` (which stays the hot-path version, used once
    per pass on the event loop) -- this one is for the export thread. A test pins that
    both give the same text; `place_findings` re-checks it at run time."""
    starts = array("I", range(len(text)))
    ends = array("I", range(1, len(text) + 1))

    if not unicodedata.is_normalized("NFC", text):
        text, starts, ends = _nfc_mapped(text, starts, ends)
    text, starts, ends = _delete_ranges(
        text,
        starts,
        ends,
        [(m.end(1), m.start(2)) for m in _LINE_BREAK_HYPHEN.finditer(text)],
    )

    text = text.translate(_FOLD)  # one character to one character
    folded = text.casefold()
    if len(folded) != len(text):  # something expanded (ß -> ss): redo it one at a time
        pieces: list[str] = []
        new_starts, new_ends = array("I"), array("I")
        for ch, s, e in zip(text, starts, ends, strict=True):
            piece = ch.casefold()
            pieces.append(piece)
            new_starts.extend([s] * len(piece))
            new_ends.extend([e] * len(piece))
        folded, starts, ends = "".join(pieces), new_starts, new_ends
    text = folded

    # Whitespace runs collapse to one space that spans the whole run.
    out: list[str] = []
    new_starts, new_ends = array("I"), array("I")
    pos = 0
    for m in _WHITESPACE.finditer(text):
        out.append(text[pos : m.start()])
        new_starts.extend(starts[pos : m.start()])
        new_ends.extend(ends[pos : m.start()])
        out.append(" ")
        new_starts.append(starts[m.start()])
        new_ends.append(ends[m.end() - 1])
        pos = m.end()
    out.append(text[pos:])
    new_starts.extend(starts[pos:])
    new_ends.extend(ends[pos:])
    text, starts, ends = "".join(out), new_starts, new_ends

    lead = len(text) - len(text.lstrip())
    trail = len(text.rstrip()) - lead
    return MappedText(
        text[lead : lead + trail], starts[lead : lead + trail], ends[lead : lead + trail]
    )


def _nfc_mapped(
    text: str, starts: array[int], ends: array[int]
) -> tuple[str, array[int], array[int]]:
    """NFC one starter-plus-combining-marks cluster at a time. A composition across two
    starters (rare scripts) is not modelled; `place_findings` catches the disagreement."""
    pieces: list[str] = []
    new_starts, new_ends = array("I"), array("I")
    i = 0
    while i < len(text):
        j = i + 1
        while j < len(text) and unicodedata.combining(text[j]):
            j += 1
        piece = unicodedata.normalize("NFC", text[i:j])
        pieces.append(piece)
        new_starts.extend([starts[i]] * len(piece))
        new_ends.extend([ends[j - 1]] * len(piece))
        i = j
    return "".join(pieces), new_starts, new_ends


def _delete_ranges(
    text: str, starts: array[int], ends: array[int], ranges: list[tuple[int, int]]
) -> tuple[str, array[int], array[int]]:
    if not ranges:
        return text, starts, ends
    pieces: list[str] = []
    new_starts, new_ends = array("I"), array("I")
    pos = 0
    for a, b in [*ranges, (len(text), len(text))]:
        pieces.append(text[pos:a])
        new_starts.extend(starts[pos:a])
        new_ends.extend(ends[pos:a])
        pos = b
    return "".join(pieces), new_starts, new_ends


def locate_quote(quote: str, source: MappedText) -> Span | None:
    """Where a finding's quote sits in the original text, by the same rules as
    `anchor_findings`. `source` is `normalize_with_map(original)`."""
    found = _locate(_normalize_quote(quote), source.text, QUOTE_MATCH_THRESHOLD)
    if found is None or found[0] >= found[1]:
        return None
    return source.original_span(*found)
