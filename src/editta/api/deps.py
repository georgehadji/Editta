"""FastAPI dependency-injection composition root. Explicit wiring, no DI framework."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from editta.adapters.llm.client import AnthropicLLMClient
from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    AuditEventRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    PassResultRepository,
    ReportRepository,
    TenantRepository,
    UserRepository,
)
from editta.adapters.persistence.uow import UnitOfWork
from editta.adapters.storage.s3 import S3BlobStore
from editta.config import Settings, load_settings
from editta.domain.ports import BlobStore, LLMClient
from editta.services.accounts import AccountService, InvalidSession

SESSION_COOKIE_NAME = "editta_session"


@dataclass(frozen=True)
class AuthContext:
    tenant_id: UUID
    user_id: UUID
    email: str


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
    return AnthropicLLMClient(
        settings.anthropic_api_key, max_concurrency=settings.max_llm_concurrency
    )


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """No RLS tenant context — only for pre-auth reads (tenant/user lookup during
    magic-link issuance and verification, before we know who's logged in)."""
    async with _app_session_factory()() as session:
        yield session


def get_account_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AccountService:
    return AccountService(
        tenants=TenantRepository(session),
        users=UserRepository(session),
        session_secret=settings.session_secret,
    )


async def get_current_auth(
    account_service: Annotated[AccountService, Depends(get_account_service)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> AuthContext:
    if session_token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    try:
        tenant_id, user_id, email = account_service.verify_session_cookie(session_token)
    except InvalidSession as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return AuthContext(tenant_id=tenant_id, user_id=user_id, email=email)


async def get_tenant_session(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
) -> AsyncIterator[AsyncSession]:
    async with uow.begin(auth.tenant_id) as session:
        yield session


def get_manuscript_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> ManuscriptRepository:
    return ManuscriptRepository(session)


def get_job_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> AnalysisJobRepository:
    return AnalysisJobRepository(session)


def get_pass_result_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> PassResultRepository:
    return PassResultRepository(session)


def get_report_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> ReportRepository:
    return ReportRepository(session)


def get_ledger_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> CreditLedgerRepository:
    return CreditLedgerRepository(session)


def get_audit_repo(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> AuditEventRepository:
    return AuditEventRepository(session)
