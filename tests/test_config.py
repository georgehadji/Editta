"""Settings load from env and fail fast when required secrets are missing."""

import os

import pytest

from editta.config import load_settings, rate_limit_storage_uri


def test_load_settings_reads_env() -> None:
    settings = load_settings()

    assert settings.environment == "production"  # secure default when unset
    assert settings.job_cost_credits == 1
    assert settings.max_llm_concurrency == 4
    assert settings.job_cost_ceiling_usd == 6.0
    assert settings.llm_requests_per_minute == 50
    assert (settings.analysis_timeout_seconds, settings.synthesis_timeout_seconds) == (1800, 900)
    assert settings.manuscript_retention_days == 30
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_load_settings_fails_fast_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDITTA_ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ValueError):
        load_settings()


def test_a_short_session_secret_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """It signs sessions and sign-in links: a guessable one lets anyone sign in as anyone."""
    monkeypatch.setenv("EDITTA_SESSION_SECRET", "too-short")

    with pytest.raises(ValueError, match="session_secret"):
        load_settings()


def test_the_example_env_placeholder_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITTA_SESSION_SECRET", "change-me-to-a-random-64-char-value")

    with pytest.raises(ValueError, match="placeholder"):
        load_settings()


def test_an_openrouter_key_alone_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDITTA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("EDITTA_OPENROUTER_API_KEY", "or-key")

    assert load_settings().openrouter_api_key == "or-key"


def test_the_rate_limit_store_is_read_without_the_full_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limiter is built at import, when the required settings may not exist."""
    for key in [k for k in os.environ if k.startswith("EDITTA_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("EDITTA_RATE_LIMIT_STORAGE_URI", "redis://cache:6379")

    assert rate_limit_storage_uri() == "redis://cache:6379"
