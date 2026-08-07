"""Application settings. Fails fast at startup if required secrets are missing."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDITTA_", env_file=".env", extra="ignore")

    database_url: str
    # BYPASSRLS role for the queue-claiming connection (infra/migrations 0002-0003).
    # Falls back to database_url for single-role local dev; production must set it.
    worker_database_url: str | None = None
    anthropic_api_key: str
    s3_endpoint_url: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    session_secret: str

    environment: str = "development"
    max_jobs_per_worker: int = 2
    max_llm_concurrency: int = 4
    job_cost_ceiling_usd: float = Field(default=6.0, gt=0)
    manuscript_retention_days: int = 30


def load_settings() -> Settings:
    return Settings()


def worker_database_url(settings: Settings) -> str:
    return settings.worker_database_url or settings.database_url
