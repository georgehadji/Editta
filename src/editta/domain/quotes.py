"""Quote anchoring: every finding an editor sees must be verbatim-traceable to the source.

Mitigates both prompt-injection-steered and hallucinated findings (PLAN.md §5). Applied
twice: to each analysis pass's findings, and to the synthesis report — the synthesis
is what the exported DOCX actually shows, so it is the output that must be guarded.
"""

from collections.abc import Iterable, Sequence

from rapidfuzz import fuzz

from editta.domain.models import Finding, SynthesisReport, SynthesisSection

QUOTE_MATCH_THRESHOLD = 0.9


def is_quote_anchored(
    quote: str, source_text: str, *, threshold: float = QUOTE_MATCH_THRESHOLD
) -> bool:
    if not quote.strip():
        return False
    return fuzz.partial_ratio(quote, source_text) / 100 >= threshold


def anchor_findings(findings: Iterable[Finding], source_text: str) -> list[Finding]:
    """Keep only findings whose quote occurs in the source text."""
    return [f for f in findings if is_quote_anchored(f.quote, source_text)]


def _traces_to_pass_finding(
    finding: Finding, pass_quotes: Sequence[str], *, threshold: float
) -> bool:
    return any(
        fuzz.ratio(finding.quote, quote) / 100 >= threshold
        or fuzz.partial_ratio(finding.quote, quote) / 100 >= threshold
        for quote in pass_quotes
    )


def anchor_synthesis(
    synthesis: SynthesisReport,
    *,
    source_text: str,
    pass_findings: Iterable[Finding],
    threshold: float = QUOTE_MATCH_THRESHOLD,
) -> SynthesisReport:
    """Drop synthesis findings that are not grounded.

    A synthesis finding survives only if (1) its quote occurs in the manuscript and
    (2) it traces back to a finding some analysis pass already produced. (1) stops
    hallucinated quotes; (2) enforces "synthesis adds no new findings" structurally
    instead of trusting the prompt to. Sections are kept even if emptied — their
    summary still orients the editor — and the overview is left untouched.
    """
    pass_quotes = [f.quote for f in pass_findings]
    sections = [
        SynthesisSection(
            title=section.title,
            summary=section.summary,
            findings=[
                f
                for f in section.findings
                if is_quote_anchored(f.quote, source_text, threshold=threshold)
                and _traces_to_pass_finding(f, pass_quotes, threshold=threshold)
            ],
        )
        for section in synthesis.sections
    ]
    return SynthesisReport(overview=synthesis.overview, sections=sections)
