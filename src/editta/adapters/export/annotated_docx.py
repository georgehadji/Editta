"""The author's own .docx with each placed finding added as a Word comment.

Nothing else changes: not one character of the text, no run is merged or restyled. A run
is cut in two only at the edge of a comment, and both halves keep the original `w:rPr`.
The editor opens the author's file and sees the findings where they belong; the author's
own comments and tracked changes are untouched.

Anything that cannot be commented without risk is left unplaced -- a few correct comments
beat many misplaced ones: text inside a field or a hyperlink, a quote that runs across a
paragraph, an edge that would cut a run holding more than plain text (a tab, a picture, a
footnote mark). Offsets come from domain/placement.py over the paragraphs that
adapters/parsing/docx.py reads, so the two cannot drift apart.
"""

import copy
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass

import docx
import structlog
from docx.oxml.ns import qn
from docx.text.hyperlink import Hyperlink
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from editta.adapters.parsing.docx import docx_body_paragraphs
from editta.adapters.parsing.safety import ManuscriptFormat, validate_upload
from editta.domain.artifacts import ArtifactKind, ExportContext
from editta.domain.models import Finding
from editta.domain.placement import place_findings

logger = structlog.get_logger(__name__)

COMMENT_AUTHOR = "Editta"
COMMENT_INITIALS = "ED"
MAX_COMMENT_CHARS = 1500

# Greek, as in the editors' own vocabulary. One table, so a tenant setting can replace it
# (open question 4 in docs/FEATURES_PLAN.md).
PASS_LABELS = {
    "metadata": "Μεταδεδομένα",
    "structure": "Δομή",
    "language": "Γλώσσα",
    "integrity": "Ακεραιότητα",
}
SUGGESTION_LABEL = "Πρόταση"
MACHINE_NOTE = "Αυτόματη παρατήρηση του Editta: η κρίση ανήκει στον επιμελητή."

# lxml refuses these in XML text, and model output is not trusted to avoid them.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def comment_text(pass_name: str, finding: Finding) -> str:
    label = PASS_LABELS.get(pass_name, pass_name)
    lines = [f"[{finding.severity.value}] {label} — {finding.issue}"]
    if finding.suggestion:
        lines.append(f"{SUGGESTION_LABEL}: {finding.suggestion}")
    text = _XML_ILLEGAL.sub("", "\n".join(lines))
    if len(text) > MAX_COMMENT_CHARS:
        text = text[: MAX_COMMENT_CHARS - 1] + "…"
    return f"{text}\n{MACHINE_NOTE}"


@dataclass(slots=True)
class _Segment:
    """A stretch of a paragraph's text and the run that holds it (None: a hyperlink)."""

    start: int
    end: int
    run: Run | None
    plain: bool  # outside any field or hyperlink: may carry a comment edge


def _segments(paragraph: Paragraph) -> list[_Segment] | None:
    """The paragraph's text laid out run by run. None if this does not add up to
    `paragraph.text`, i.e. python-docx and this walk disagree about the paragraph."""
    segments: list[_Segment] = []
    position = 0
    field_depth = 0
    for item in paragraph.iter_inner_content():
        length = len(item.text)
        if isinstance(item, Hyperlink):
            segments.append(_Segment(position, position + length, None, plain=False))
        else:
            field_marks = item._r.xpath("w:fldChar/@w:fldCharType")
            in_field = field_depth > 0 or bool(field_marks)
            # shortcut: a field that starts in one paragraph and ends in a later one is
            # not tracked across the break; its tail reads as plain text. Rare (a TOC).
            field_depth = max(
                0, field_depth + field_marks.count("begin") - field_marks.count("end")
            )
            segments.append(_Segment(position, position + length, item, plain=not in_field))
        position += length
    return segments if position == len(paragraph.text) else None


def _only_text(run: Run) -> bool:
    """A run of one `w:t` and formatting: the only kind this cuts in two."""
    kids = [child for child in run._r if child.tag != qn("w:rPr")]
    return len(kids) == 1 and kids[0].tag == qn("w:t")


def _split_at(paragraph: Paragraph, offset: int) -> None:
    for segment in _segments(paragraph) or []:
        if segment.start < offset < segment.end and segment.run is not None:
            element = segment.run._r
            text_element = element.find(qn("w:t"))
            text = text_element.text or ""
            cut = offset - segment.start
            right = copy.deepcopy(element)
            element.addnext(right)
            right_text = right.find(qn("w:t"))
            text_element.text, right_text.text = text[:cut], text[cut:]
            for node in (text_element, right_text):
                node.set(qn("xml:space"), "preserve")
            return


def _runs_for_span(paragraph: Paragraph, start: int, end: int) -> list[Run] | None:
    """The runs holding exactly [start, end) of the paragraph's text, cutting the runs at
    the two edges; None (and nothing changed) if that cannot be done safely."""
    segments = _segments(paragraph)
    if segments is None:
        return None
    covered = [s for s in segments if s.end > s.start and s.start < end and s.end > start]
    if not covered or any(not s.plain or s.run is None for s in covered):
        return None
    first, last = covered[0], covered[-1]
    for segment, cut in ((first, start > first.start), (last, end < last.end)):
        if cut and (segment.run is None or not _only_text(segment.run)):
            return None
    _split_at(paragraph, end)
    _split_at(paragraph, start)
    runs = [
        s.run
        for s in _segments(paragraph) or []
        if s.run is not None and s.end > s.start and s.start >= start and s.end <= end
    ]
    return runs or None


class AnnotatedManuscriptBuilder:
    def __init__(self, source: bytes) -> None:
        self._document = docx.Document(io.BytesIO(source))

    def build(self, manuscript_text: str, findings: Sequence[tuple[str, Finding]]) -> bytes | None:
        """The commented file, or None when there is nothing to comment or the paragraphs
        here are not the ones the analysis read."""
        paragraphs = docx_body_paragraphs(self._document)
        raw_texts = [p.text for p in paragraphs]
        texts = [t.strip() for t in raw_texts]
        if "\n".join(texts) != manuscript_text:
            logger.warning("export.annotated_text_mismatch")
            return None

        result = place_findings(texts, [finding for _, finding in findings])
        added = 0
        for placement in result.placed:
            raw = raw_texts[placement.paragraph]
            lead = len(raw) - len(raw.lstrip())  # extract_docx strips; offsets are stripped
            pass_name, finding = findings[placement.finding]
            paragraph = paragraphs[placement.paragraph]
            runs = _runs_for_span(paragraph, lead + placement.start, lead + placement.end)
            if runs is None:
                continue
            self._document.add_comment(
                runs,
                text=comment_text(pass_name, finding),
                author=COMMENT_AUTHOR,
                initials=COMMENT_INITIALS,
            )
            added += 1
        logger.info("export.annotated", comments=added, not_placed=len(findings) - added)
        if not added:
            return None
        buffer = io.BytesIO()
        self._document.save(buffer)
        return buffer.getvalue()


class AnnotatedManuscriptExporter:
    """The findings as Word comments in the author's file (an ArtifactExporter). A bonus
    on top of the report: whatever goes wrong here yields no file, never a failed job."""

    kind = ArtifactKind.ANNOTATED_MANUSCRIPT

    def build(self, ctx: ExportContext) -> bytes | None:
        try:
            fmt = validate_upload(ctx.source, ctx.manuscript.source_filename)
            if fmt is not ManuscriptFormat.DOCX:
                return None  # a PDF has no comments to add
            findings = [(r.pass_name, f) for r in ctx.pass_results for f in r.findings]
            return AnnotatedManuscriptBuilder(ctx.source).build(ctx.manuscript.text, findings)
        except Exception as exc:
            # Type only: the message may quote the manuscript (PLAN.md §5).
            logger.warning("export.annotated_failed", error_type=type(exc).__name__)
            return None
