from editta.domain.models import Finding, Severity
from editta.domain.placement import place_findings

PARAGRAPHS = [
    "Πρώτη παράγραφος με ένα σφάλμα εδώ.",
    "Δεύτερη παράγραφος, και λίγο παρακάτω η λέξη κειμενο χωρίς τόνο.",
    "Τρίτη παράγραφος.",
]


def _finding(quote: str) -> Finding:
    return Finding(section="s", quote=quote, issue="i", severity=Severity.MINOR, suggestion="fix")


def test_a_quote_is_placed_in_its_paragraph_with_offsets_into_that_paragraph() -> None:
    result = place_findings(PARAGRAPHS, [_finding("κειμενο")])
    (placement,) = result.placed
    assert placement.paragraph == 1
    assert PARAGRAPHS[1][placement.start : placement.end] == "κειμενο"
    assert result.unplaced == ()


def test_findings_keep_their_input_index() -> None:
    result = place_findings(PARAGRAPHS, [_finding("δεν υπάρχει"), _finding("σφάλμα")])
    assert [p.finding for p in result.placed] == [1]
    assert result.unplaced == (0,)


def test_a_quote_that_runs_across_two_paragraphs_is_not_placed() -> None:
    across = _finding("σφάλμα εδώ. Δεύτερη παράγραφος")
    result = place_findings(PARAGRAPHS, [across])
    assert result.placed == ()
    assert result.unplaced == (0,)


def test_an_ambiguous_short_quote_is_not_placed() -> None:
    result = place_findings(["παράγραφος", "παράγραφος"], [_finding("παράγραφος")])
    assert result.placed == ()


def test_the_first_and_last_paragraph_are_reachable() -> None:
    result = place_findings(PARAGRAPHS, [_finding("Πρώτη"), _finding("Τρίτη παράγραφος")])
    assert [(p.paragraph, p.start) for p in result.placed] == [(0, 0), (2, 0)]


def test_no_paragraphs_places_nothing() -> None:
    result = place_findings([], [_finding("κάτι")])
    assert result.placed == ()
    assert result.unplaced == (0,)
