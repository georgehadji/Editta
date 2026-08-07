"""Request/response DTOs and the {success, data, error, meta} response envelope."""

from uuid import UUID

from pydantic import BaseModel, EmailStr


class Envelope[T](BaseModel):
    success: bool
    data: T | None = None
    error: str | None = None
    meta: dict[str, object] | None = None

    @classmethod
    def ok(cls, data: T, *, meta: dict[str, object] | None = None) -> "Envelope[T]":
        return cls(success=True, data=data, error=None, meta=meta)

    @classmethod
    def fail(cls, error: str) -> "Envelope[None]":
        return Envelope[None](success=False, data=None, error=error, meta=None)


class MagicLinkRequest(BaseModel):
    tenant_id: UUID
    email: EmailStr


class MagicLinkRequestResponse(BaseModel):
    # Dev/local convenience only: a real deploy emails this token instead of
    # returning it (no transactional-email provider is wired yet — see accounts.py).
    magic_link_token: str


class MagicLinkVerifyRequest(BaseModel):
    token: str


class SessionResponse(BaseModel):
    tenant_id: UUID
    user_id: UUID
    email: str


class ManuscriptUploadResponse(BaseModel):
    manuscript_id: UUID
    job_id: UUID


class JobStatusResponse(BaseModel):
    job_id: UUID
    state: str
    error: str | None = None


class ReportDownloadResponse(BaseModel):
    url: str
    expires_in: int
