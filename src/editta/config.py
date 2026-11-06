"""Application settings. Fails fast at startup if required secrets are missing."""

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDITTA_", env_file=".env", extra="ignore")

    database_url: str
    # BYPASSRLS role for the queue-claiming connection (infra/migrations 0002-0003).
    # Falls back to database_url for single-role local dev; production must set it.
    worker_database_url: str | None = None
    # One of the two is required; with both, OpenRouter serves the model calls and
    # Anthropic only counts tokens (adapters/llm/__init__.py).
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    s3_endpoint_url: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    # Signs session cookies and sign-in links: whoever knows it can sign in as anyone.
    session_secret: str = Field(min_length=32)

    # Secure by default: development-only conveniences (console email sender, sign-in
    # links shown on the page) require opting in with EDITTA_ENVIRONMENT=development.
    environment: str = "production"
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

    @field_validator("session_secret")
    @classmethod
    def _not_the_example_secret(cls, value: str) -> str:
        if value.startswith("change-me"):  # the placeholder in .env.example
            raise ValueError("EDITTA_SESSION_SECRET is still the .env.example placeholder")
        return value

    @model_validator(mode="after")
    def _require_an_llm_key(self) -> "Settings":
        if not (self.anthropic_api_key or self.openrouter_api_key):
            raise ValueError("set EDITTA_OPENROUTER_API_KEY or EDITTA_ANTHROPIC_API_KEY")
        return self


def load_settings() -> Settings:
    return Settings()


def worker_database_url(settings: Settings) -> str:
    return settings.worker_database_url or settings.database_url
