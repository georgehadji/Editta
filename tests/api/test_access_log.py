import logging

from editta.api.main import _RedactTokens


def _access_line(path: str) -> str:
    # uvicorn's access-log arguments: client, method, full path, http version, status.
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", path, "1.1", 200), None,
    )  # fmt: skip
    assert _RedactTokens().filter(record)
    return record.getMessage()


def test_sign_in_token_is_redacted_but_the_rest_of_the_line_is_kept() -> None:
    line = _access_line("/t/acme/auth/click?token=abc.def-ghi&next=%2F")

    assert "abc.def-ghi" not in line
    assert "token=REDACTED&next=%2F" in line
    assert '"GET /t/acme/auth/click' in line


def test_lines_without_a_token_are_untouched() -> None:
    assert "/healthz?x=1" in _access_line("/healthz?x=1")
