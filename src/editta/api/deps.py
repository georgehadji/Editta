"""FastAPI dependency-injection composition root. Explicit wiring, no DI framework."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from uuid import UUID

import stripe
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from editta.adapters.email.console import ConsoleEmailSender
from editta.adapters.email.smtp import SMTPEmailSender
from editta.adapters.llm import build_llm_client
from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    ReportRepository,
    TenantRepository,
    UserRepository,
)
from editta.adapters.persistence.uow import UnitOfWork
from editta.adapters.storage.s3 import S3BlobStore
from editta.config import Settings, load_settings
from editta.domain.ports import BlobStore, EmailSender, LLMClient
from editta.services.accounts import AccountService, AuthenticatedUser, InvalidSession
from editta.services.billing import BillingService

SESSION_COOKIE_NAME = "editta_session"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@dataclass(frozen=True)
class AuthContext:
    tenant_id: UUID
    user_id: UUID
    email: str
    role: str = "editor"

    def as_user(self) -> AuthenticatedUser:
        return AuthenticatedUser(self.tenant_id, self.user_id, self.email, self.role)


@lru_cache
def get_settings() -> Settings:
    return load_settings()


@lru_cache
def _app_session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    return async_sessionmaker(engine, expire_on_commit=False)


@lru_cache
def get_uow() -> UnitOfWork:
    return UnitOfWork(_app_session_factory())


@lru_cache
def get_blob_store() -> BlobStore:
    settings = get_settings()
    return S3BlobStore(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )


@lru_cache
def get_llm_client() -> LLMClient:
    settings = get_settings()
    return build_llm_client(
        anthropic_api_key=settings.anthropic_api_key,
        openrouter_api_key=settings.openrouter_api_key,
        max_concurrency=settings.max_llm_concurrency,
    )


def get_account_service(
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AccountService:
    """Every user lookup — including the pre-authentication ones — runs in its own
    tenant-scoped transaction, so row-level security applies to login too."""

    @asynccontextmanager
    async def users(tenant_id: UUID) -> AsyncIterator[UserRepository]:
        async with uow.begin(tenant_id) as session:
            yield UserRepository(session)

    return AccountService(users=users, session_secret=settings.session_secret)


async def get_current_auth(
    account_service: Annotated[AccountService, Depends(get_account_service)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> AuthContext:
    if session_token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    try:
        user = await account_service.authenticate_session(session_token)
    except InvalidSession as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return AuthContext(
        tenant_id=user.tenant_id, user_id=user.user_id, email=user.email, role=user.role
    )


async def get_tenant_session(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
) -> AsyncIterator[AsyncSession]:
    """One tenant-scoped transaction per request. Every repository dependency declares
    it with scope="function", so the transaction commits when the endpoint returns and
    *before* the response is sent: a client never receives a job id (or a 201) for a
    write that then fails to commit. With the default request scope, the commit ran
    after the response body had already gone out."""
    async with uow.begin(auth.tenant_id) as session:
        yield session


def get_manuscript_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session, scope="function")],
) -> ManuscriptRepository:
    return ManuscriptRepository(session)


def get_job_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session, scope="function")],
) -> AnalysisJobRepository:
    return AnalysisJobRepository(session)


def get_report_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session, scope="function")],
) -> ReportRepository:
    return ReportRepository(session)


def get_ledger_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session, scope="function")],
) -> CreditLedgerRepository:
    return CreditLedgerRepository(session)


def get_audit_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session, scope="function")],
) -> AuditEventRepository:
    return AuditEventRepository(session)


def get_tenant_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session, scope="function")],
) -> TenantRepository:
    return TenantRepository(session)


@lru_cache
def get_templates() -> Jinja2Templates:
    return Jinja2Templates(directory=TEMPLATES_DIR)


def get_email_sender() -> EmailSender:
    """SMTP when configured. The console sender (logs the link) exists for local
    development only: in any other environment a missing SMTP config is an error, not
    a silent fallback — a login link that is not emailed can be used by whoever sees it.
    """
    settings = get_settings()
    if settings.smtp_host and settings.smtp_username and settings.smtp_password:
        return _smtp_sender(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_username,
            settings.smtp_password,
            settings.smtp_from_address,
        )
    if settings.environment == "development":
        return ConsoleEmailSender()
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "email delivery is not configured")


@lru_cache
def _smtp_sender(
    host: str, port: int, username: str, password: str, from_address: str
) -> EmailSender:
    return SMTPEmailSender(
        host=host, port=port, username=username, password=password, from_address=from_address
    )


@lru_cache
def get_billing_service() -> BillingService:
    settings = get_settings()
    secret_key, webhook_secret, price_id = (
        settings.stripe_secret_key,
        settings.stripe_webhook_secret,
        settings.stripe_price_id,
    )
    if not secret_key or not webhook_secret or not price_id:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "billing is not configured")
    return BillingService(
        client=stripe.StripeClient(secret_key),
        webhook_secret=webhook_secret,
        price_id=price_id,
        credits_per_purchase=settings.credits_per_purchase,
        success_url=f"{settings.app_base_url}/billing/success",
        cancel_url=f"{settings.app_base_url}/billing/cancel",
    )
