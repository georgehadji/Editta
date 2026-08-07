"""Settings load from env and fail fast when required secrets are missing."""

import pytest

from editta.config import load_settings


def test_load_settings_reads_env() -> None:
    settings = load_settings()

    assert settings.environment == "development"
    assert settings.max_jobs_per_worker == 2
    assert settings.max_llm_concurrency == 4
    assert settings.job_cost_ceiling_usd == 6.0
    assert settings.manuscript_retention_days == 30
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_load_settings_fails_fast_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDITTA_ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ValueError):
        load_settings()
