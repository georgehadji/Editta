import sys
from pathlib import Path

import pytest

from editta import cli


def test_analyze_takes_the_api_key_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("EDITTA_ANTHROPIC_API_KEY", "EDITTA_OPENROUTER_API_KEY"):
        monkeypatch.setenv(name, "x")  # so teardown restores what load_dotenv sets
        monkeypatch.delenv(name)
    (tmp_path / ".env").write_text("EDITTA_ANTHROPIC_API_KEY=from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["editta", "analyze", "missing.docx", "-o", "r.docx"])

    with pytest.raises(SystemExit):
        cli.main()

    # Past the key check: it fails on the missing file instead.
    assert "failed to read missing.docx" in capsys.readouterr().err
