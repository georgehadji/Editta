import pytest
from fastapi import HTTPException

from editta.adapters.email.console import ConsoleEmailSender
from editta.api import deps
from editta.services.accounts import AuthenticatedUser


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


def test_a_failed_commit_is_an_error_response_not_a_success_already_sent() -> None:
    """The request transaction must commit *before* the response goes out. Previously
    (request-scoped dependency) the commit ran after the body was sent, so a client
    could get a 201 + job id for a write that then failed to commit."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from editta.api.main import app

    class FailingCommitUow:
        @asynccontextmanager
        async def begin(self, tenant_id):  # noqa: ANN001, ANN202
            job = SimpleNamespace(id=uuid4(), state="pending", error=None)

            async def execute(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
                return SimpleNamespace(scalar_one_or_none=lambda: job)

            yield SimpleNamespace(execute=execute)
            raise RuntimeError("commit failed")

    tenant_id = uuid4()
    app.dependency_overrides[deps.get_uow] = FailingCommitUow
    app.dependency_overrides[deps.get_current_auth] = lambda: AuthenticatedUser(
        tenant_id=tenant_id, user_id=uuid4(), email="e@example.com", role="editor"
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(f"/manuscripts/jobs/{uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500  # not the 200 the endpoint itself produced
