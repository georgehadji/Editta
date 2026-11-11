"""The one logging setup: what a line carries, and what it must never carry."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from editta.observability import MAX_FIELD_CHARS, configure_logging


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_a_line_names_its_service_and_carries_no_manuscript_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("worker")

    structlog.get_logger().info(
        "job.done",
        text="Ἡ ψυχὴ τοῦ Σωκράτους",
        content="more of it",
        passage="y" * (MAX_FIELD_CHARS + 1),
        raw=b"abc",
        job_id="j1",
    )

    (line,) = _lines(capsys)
    assert (line["service"], line["level"], line["job_id"]) == ("worker", "info", "j1")
    assert "timestamp" in line
    assert "text" not in line and "content" not in line
    assert line["passage"].startswith("[dropped") and line["raw"].startswith("[dropped")


def test_bound_ids_appear_on_lines_written_inside_the_block_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("api")

    with structlog.contextvars.bound_contextvars(request_id="r1", job_id="j1"):
        structlog.get_logger().info("inside")
    structlog.get_logger().info("outside")

    inside, outside = _lines(capsys)
    assert (inside["request_id"], inside["job_id"]) == ("r1", "j1")
    assert "request_id" not in outside


def test_a_logged_exception_says_what_failed_and_where_but_not_its_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("worker")

    try:
        raise ValueError("Ἡ ψυχὴ τοῦ Σωκράτους")  # an exception that quotes the manuscript
    except ValueError:
        structlog.get_logger().exception("job.attempt_crashed")

    (line,) = _lines(capsys)
    assert line["error_type"] == "ValueError"
    assert line["error_at"].endswith(f"test_observability.py:{_raise_line()}")
    assert "exc_info" not in line and "Σωκράτους" not in json.dumps(line, ensure_ascii=False)


def _raise_line() -> int:
    source = Path(__file__).read_text("utf-8").splitlines()
    return next(i for i, text in enumerate(source, 1) if 'raise ValueError("Ἡ' in text)


def test_structlog_is_configured_in_exactly_one_place() -> None:
    """A second setup could leave a process without the scrub, as the worker once was."""
    src = Path(__file__).resolve().parents[1] / "src"
    owners = [
        path.name for path in src.rglob("*.py") if "structlog.configure(" in path.read_text("utf-8")
    ]

    assert owners == ["observability.py"]
