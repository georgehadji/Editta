"""Shared pytest fixtures."""

import os

import pytest

from editta.config import Settings

_REQUIRED_ENV = {
    "EDITTA_DATABASE_URL": "postgresql+asyncpg://editta:editta@localhost:5432/editta",
    "EDITTA_ANTHROPIC_API_KEY": "test-key",
    "EDITTA_S3_ENDPOINT_URL": "http://localhost:9000",
    "EDITTA_S3_BUCKET": "editta-test",
    "EDITTA_S3_ACCESS_KEY": "test",
    "EDITTA_S3_SECRET_KEY": "test",
    "EDITTA_SESSION_SECRET": "test-session-secret-at-least-32-chars",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer's real .env and shell must not decide what a test sees.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in [k for k in os.environ if k.startswith("EDITTA_")]:
        monkeypatch.delenv(key)
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> None:
    # The limiter keeps its counters in module state; a test must not inherit another's.
    from editta.api.middleware import limiter

    limiter.reset()
