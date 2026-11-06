"""Which paragraph, and where in it, each finding sits. Pure: no Word, no I/O.

Used by the annotated-manuscript export (adapters/export/annotated_docx.py). A finding is
placed only when its quote is found by the same rule that let it through the anchoring
gate (domain/quotes.py) AND lies inside one paragraph. A position is never guessed: what
cannot be placed is returned as unplaced, for the caller to count and report.
"""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from editta.domain.models import Finding
from editta.domain.quotes import locate_quote, normalize_text, normalize_with_map


@dataclass(frozen=True, slots=True)
class Placement:
    finding: int  # index into the findings given to place_findings
    paragraph: int  # index into the paragraph texts
    start: int  # [start, end) into that paragraph's text
    end: int


@dataclass(frozen=True, slots=True)
class PlacementResult:
    placed: tuple[Placement, ...]
    unplaced: tuple[int, ...]  # indexes into the findings


def place_findings(paragraph_texts: Sequence[str], findings: Sequence[Finding]) -> PlacementResult:
    """`paragraph_texts` must be exactly what the analysis read, one string per paragraph
    (ManuscriptText.text is these joined with "\\n")."""
    joined = "\n".join(paragraph_texts)
    mapped = normalize_with_map(joined)
    if mapped.text != normalize_text(joined):  # the two normalizers disagree: trust neither
        return PlacementResult(placed=(), unplaced=tuple(range(len(findings))))

    starts: list[int] = []
    offset = 0
    for text in paragraph_texts:
        starts.append(offset)
        offset += len(text) + 1  # the "\n"

    placed: list[Placement] = []
    unplaced: list[int] = []
    for index, finding in enumerate(findings):
        span = locate_quote(finding.quote, mapped)
        if span is None:
            unplaced.append(index)
            continue
        paragraph = bisect_right(starts, span.start) - 1  # the paragraph it starts in
        base = starts[paragraph]
        if span.end - base > len(paragraph_texts[paragraph]):  # runs on into the next one
            unplaced.append(index)
            continue
        placed.append(Placement(index, paragraph, span.start - base, span.end - base))
    return PlacementResult(placed=tuple(placed), unplaced=tuple(unplaced))
