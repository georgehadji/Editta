"""Request/response DTOs and the {success, data, error} envelope of a successful
response. Errors are FastAPI's own {"detail": ...} body, not an envelope."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr


class Envelope[T](BaseModel):
    success: bool
    data: T | None = None
    error: str | None = None

    @classmethod
    def ok(cls, data: T) -> "Envelope[T]":
        return cls(success=True, data=data, error=None)


class MagicLinkRequest(BaseModel):
    tenant_id: UUID
    email: EmailStr


class MagicLinkRequestResponse(BaseModel):
    """Always the same response, whether or not the email has an account: the link is
    only ever delivered by email, and the endpoint must not reveal who is a user."""

    accepted: bool = True


class InviteRequest(BaseModel):
    email: EmailStr
    role: Literal["admin", "editor"] = "editor"


class InviteResponse(BaseModel):
    user_id: UUID
    email: str
    role: str


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
