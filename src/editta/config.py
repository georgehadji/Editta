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

    # Secure by default: development-only conveniences (console email sender, sign-in
    # links shown on the page) require opting in with EDITTA_ENVIRONMENT=development.
    environment: str = "production"
    max_jobs_per_worker: int = 2
    max_llm_concurrency: int = 4
    job_cost_ceiling_usd: float = Field(default=6.0, gt=0)
    job_cost_credits: int = Field(default=1, ge=1)
    manuscript_retention_days: int = 30

    app_base_url: str = "http://localhost:8000"

    # SMTP is optional: unset means adapters/email/console.py is used (dev-only,
    # logs the magic link instead of sending it — see api/deps.py).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str = "noreply@editta.app"

    # Stripe is optional too: billing routes 503 until these are set.
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_id: str | None = None
    credits_per_purchase: int = 10


def load_settings() -> Settings:
    return Settings()


def worker_database_url(settings: Settings) -> str:
    return settings.worker_database_url or settings.database_url
