from editta.domain.models import Finding, Severity, SynthesisReport, SynthesisSection
from editta.domain.quotes import anchor_findings, anchor_synthesis, is_quote_anchored

SOURCE = "Η γλώσσα του κειμένου παρουσιάζει επαναλήψεις στο δεύτερο κεφάλαιο."


def test_exact_quote_is_anchored() -> None:
    assert is_quote_anchored("επαναλήψεις στο δεύτερο κεφάλαιο", SOURCE)


def test_fabricated_quote_is_not_anchored() -> None:
    assert not is_quote_anchored("αυτό το απόσπασμα δεν υπάρχει πουθενά στο κείμενο", SOURCE)


def test_empty_quote_is_not_anchored() -> None:
    assert not is_quote_anchored("   ", SOURCE)


def _finding(quote: str) -> Finding:
    return Finding(section="ch2", quote=quote, issue="i", severity=Severity.MINOR, suggestion="s")


def test_anchor_findings_drops_fabricated_quotes() -> None:
    real, fake = _finding("επαναλήψεις στο δεύτερο"), _finding("δεν υπάρχει πουθενά εδώ μέσα")
    assert anchor_findings([real, fake], SOURCE) == [real]


def test_anchor_synthesis_keeps_findings_traced_to_a_pass() -> None:
    pass_finding = _finding("επαναλήψεις στο δεύτερο κεφάλαιο")
    synthesis = SynthesisReport(
        overview="o",
        sections=[SynthesisSection(title="Γλώσσα", summary="s", findings=[pass_finding])],
    )

    result = anchor_synthesis(synthesis, source_text=SOURCE, pass_findings=[pass_finding])

    assert result.sections[0].findings == [pass_finding]


def test_anchor_synthesis_drops_hallucinated_quote() -> None:
    synthesis = SynthesisReport(
        overview="o",
        sections=[
            SynthesisSection(
                title="t", summary="s", findings=[_finding("ένα απόσπασμα που επινοήθηκε")]
            )
        ],
    )
    result = anchor_synthesis(synthesis, source_text=SOURCE, pass_findings=[])
    assert result.sections[0].findings == []
    assert result.sections[0].summary == "s"  # the section and its summary survive


def test_anchor_synthesis_drops_real_quote_no_pass_reported() -> None:
    """Grounded in the text but new: synthesis may not add findings (PLAN.md §5)."""
    new_but_real = _finding("Η γλώσσα του κειμένου")
    reported = _finding("επαναλήψεις στο δεύτερο κεφάλαιο")
    synthesis = SynthesisReport(
        overview="o",
        sections=[SynthesisSection(title="t", summary="s", findings=[new_but_real, reported])],
    )

    result = anchor_synthesis(synthesis, source_text=SOURCE, pass_findings=[reported])

    assert result.sections[0].findings == [reported]
