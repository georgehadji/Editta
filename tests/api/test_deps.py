import pytest
from fastapi import HTTPException

from editta.adapters.email.console import ConsoleEmailSender
from editta.api import deps


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    deps.get_settings.cache_clear()


def test_production_without_smtp_refuses_instead_of_logging_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console sender makes a sign-in link visible to whoever reads logs or the
    page; outside development a missing SMTP config must fail loudly."""
    monkeypatch.setenv("EDITTA_ENVIRONMENT", "production")

    with pytest.raises(HTTPException) as exc_info:
        deps.get_email_sender()

    assert exc_info.value.status_code == 503


def test_development_without_smtp_uses_the_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITTA_ENVIRONMENT", "development")
    assert isinstance(deps.get_email_sender(), ConsoleEmailSender)
