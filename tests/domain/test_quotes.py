from editta.domain.quotes import is_quote_anchored

SOURCE = "Η γλώσσα του κειμένου παρουσιάζει επαναλήψεις στο δεύτερο κεφάλαιο."


def test_exact_quote_is_anchored() -> None:
    assert is_quote_anchored("επαναλήψεις στο δεύτερο κεφάλαιο", SOURCE)


def test_fabricated_quote_is_not_anchored() -> None:
    assert not is_quote_anchored("αυτό το απόσπασμα δεν υπάρχει πουθενά στο κείμενο", SOURCE)


def test_empty_quote_is_not_anchored() -> None:
    assert not is_quote_anchored("   ", SOURCE)
